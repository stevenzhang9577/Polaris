"""Paper detail extras stay identical and library-scoped across route families."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import new_paper
from app.models.user import User
from app.models.zotero_local import ZoteroItemLink, ZoteroLocalBinding
from tests.conftest import register_and_login


async def _login(client, email: str) -> tuple[dict[str, str], uuid.UUID]:
    token = await register_and_login(client, email=email)
    headers = {"Authorization": f"Bearer {token}"}
    async with get_sessionmaker()() as session:
        user_id = await session.scalar(select(User.id).where(User.email == email))
    assert user_id is not None
    return headers, user_id


def _assert_zotero_context(
    body: dict,
    *,
    library_id: uuid.UUID,
    item_key: str,
    can_materialize: bool,
    can_manage_summary: bool,
) -> None:
    assert body["zotero_source"] is True
    assert body["zotero_library_id"] == str(library_id)
    assert body["zotero_item_key"] == item_key
    assert body["zotero_pdf_status"] == "linked"
    assert body["can_materialize_zotero"] is can_materialize
    assert body["can_manage_summary"] is can_manage_summary
    assert body["pdf_available"] is can_materialize


async def test_library_and_global_details_share_contextual_zotero_and_summary_permissions(
    client,
):
    public_owner_headers, public_owner_id = await _login(
        client, "paper-read-public-owner@example.com"
    )
    private_owner_headers, private_owner_id = await _login(
        client, "paper-read-private-owner@example.com"
    )
    stranger_headers, _stranger_id = await _login(client, "paper-read-stranger@example.com")

    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        public_library = DirectionLibrary(
            name="Public contextual library",
            is_public=True,
            submitted_by=public_owner_id,
        )
        private_library = DirectionLibrary(
            name="Private contextual library",
            is_public=False,
            submitted_by=private_owner_id,
        )
        paper = new_paper(title="One paper in two Zotero libraries", source="zotero")
        session.add_all([public_library, private_library, paper])
        await session.flush()
        session.add_all(
            [
                LibraryPaper(
                    library_id=public_library.id,
                    paper_id=paper.id,
                    status="included",
                    created_at=now,
                ),
                LibraryPaper(
                    library_id=private_library.id,
                    paper_id=paper.id,
                    status="included",
                    created_at=now + timedelta(seconds=1),
                ),
            ]
        )
        public_binding = ZoteroLocalBinding(
            library_id=public_library.id,
            created_by=public_owner_id,
            collection_key="PUBLIC",
            collection_name="Public collection",
        )
        private_binding = ZoteroLocalBinding(
            library_id=private_library.id,
            created_by=private_owner_id,
            collection_key="PRIVATE",
            collection_name="Private collection",
        )
        session.add_all([public_binding, private_binding])
        await session.flush()
        session.add_all(
            [
                ZoteroItemLink(
                    binding_id=public_binding.id,
                    item_key="PUBLIC-ITEM",
                    item_version=1,
                    paper_id=paper.id,
                    status="active",
                    attachment_key="PUBLIC-PDF",
                    pdf_status="linked",
                ),
                ZoteroItemLink(
                    binding_id=private_binding.id,
                    item_key="PRIVATE-ITEM",
                    item_version=1,
                    paper_id=paper.id,
                    status="active",
                    attachment_key="PRIVATE-PDF",
                    pdf_status="linked",
                ),
            ]
        )
        await session.commit()
        public_library_id = public_library.id
        private_library_id = private_library.id
        paper_id = paper.id

    # Each owner sees only the link belonging to the exact library represented by the view.
    for headers, library_id, item_key in (
        (public_owner_headers, public_library_id, "PUBLIC-ITEM"),
        (private_owner_headers, private_library_id, "PRIVATE-ITEM"),
    ):
        library_detail = await client.get(
            f"/api/libraries/{library_id}/papers/{paper_id}", headers=headers
        )
        assert library_detail.status_code == 200, library_detail.text
        _assert_zotero_context(
            library_detail.json(),
            library_id=library_id,
            item_key=item_key,
            can_materialize=True,
            can_manage_summary=True,
        )

        global_detail = await client.get(f"/api/papers/{paper_id}", headers=headers)
        assert global_detail.status_code == 200, global_detail.text
        _assert_zotero_context(
            global_detail.json(),
            library_id=library_id,
            item_key=item_key,
            can_materialize=True,
            can_manage_summary=True,
        )

    # Public read access exposes only that public library's context.  It does not grant the
    # local-file capability or shared-summary writes, and the private library stays hidden.
    public_detail = await client.get(
        f"/api/libraries/{public_library_id}/papers/{paper_id}", headers=stranger_headers
    )
    assert public_detail.status_code == 200, public_detail.text
    _assert_zotero_context(
        public_detail.json(),
        library_id=public_library_id,
        item_key="PUBLIC-ITEM",
        can_materialize=False,
        can_manage_summary=False,
    )
    global_detail = await client.get(f"/api/papers/{paper_id}", headers=stranger_headers)
    assert global_detail.status_code == 200, global_detail.text
    _assert_zotero_context(
        global_detail.json(),
        library_id=public_library_id,
        item_key="PUBLIC-ITEM",
        can_materialize=False,
        can_manage_summary=False,
    )
    hidden = await client.get(
        f"/api/libraries/{private_library_id}/papers/{paper_id}", headers=stranger_headers
    )
    assert hidden.status_code == 404
    denied = await client.post(f"/api/papers/{paper_id}/recompile", headers=stranger_headers)
    assert denied.status_code == 404

    # Explicitly linking the public library to the caller's project grants the documented
    # shared-summary privilege, but still does not grant access to its local Zotero file.
    project = await client.post(
        "/api/projects",
        json={"name": "Stranger research context"},
        headers=stranger_headers,
    )
    assert project.status_code == 201, project.text
    linked = await client.put(
        f"/api/projects/{project.json()['id']}/source-libraries",
        json={"library_ids": [str(public_library_id)]},
        headers=stranger_headers,
    )
    assert linked.status_code == 200, linked.text

    for url in (
        f"/api/libraries/{public_library_id}/papers/{paper_id}",
        f"/api/papers/{paper_id}",
    ):
        response = await client.get(url, headers=stranger_headers)
        assert response.status_code == 200, response.text
        _assert_zotero_context(
            response.json(),
            library_id=public_library_id,
            item_key="PUBLIC-ITEM",
            can_materialize=False,
            can_manage_summary=True,
        )
