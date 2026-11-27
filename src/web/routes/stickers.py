import re
from base64 import b64encode
from contextlib import aclosing

from quart import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    make_response,
    render_template,
    request,
)

from src.adapters.telegram.media import MediaBusy, MediaTooLarge
from src.adapters.telegram.sticker_catalog import LISTS
from src.adapters.telegram.sticker_rpc import StickerError
from src.container import _get_tg_adapter
from src.domain.errors import AccountChanged
from src.web.requests import require_account_token

stickers_bp = Blueprint("stickers", __name__)
PACK_ID = re.compile(r"[1-9][0-9]{0,18}")
SLUG = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def workspace():
    adapter = _get_tg_adapter()
    if request.path.startswith("/api/"):
        require_account_token(adapter)
    service = adapter.stickers
    service.check()
    return service


def pack_id(value):
    if (
        not isinstance(value, str)
        or not PACK_ID.fullmatch(value)
        or int(value) >= 2**63
    ):
        raise StickerError("Invalid pack or sticker ID.")
    return value


def source_id(value):
    return value if value in ("favorites", "recent") else pack_id(value)


def list_of(values, parse, limit):
    if not isinstance(values, list) or not 0 < len(values) <= limit:
        raise StickerError(f"Select between 1 and {limit} items.")
    return list(dict.fromkeys(parse(value) for value in values))


def entry(value):
    if not isinstance(value, list) or len(value) != 2:
        raise StickerError("Invalid sticker selection.")
    return source_id(value[0]), int(pack_id(value[1]))


def title(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 64:
        raise StickerError("Enter a pack title with 1–64 characters.")
    return value.strip()


def parse_operation(body):
    kind = body.get("kind")
    if kind in ("scan", "uninstall"):
        return kind, {
            "sources": list_of(
                body.get("sources"), pack_id, 250 if kind == "scan" else 1000
            )
        }
    if kind == "install":
        return kind, {"source": pack_id(body.get("source"))}
    if kind == "rename":
        return kind, {
            "source": pack_id(body.get("source")),
            "title": title(body.get("title")),
        }
    if kind == "reorder":
        source = body.get("source")
        if source != "installed":
            source = pack_id(source)
        before = list_of(body.get("before"), lambda value: int(pack_id(value)), 1000)
        order = list_of(body.get("order"), lambda value: int(pack_id(value)), 1000)
        moved = int(pack_id(body.get("moved")))
        if set(before) != set(order) or moved not in before:
            raise StickerError("Submit the complete order with the same items.")
        return kind, dict(source=source, before=before, order=order, moved=moved)
    if kind in ("copy", "edit"):
        entries = list_of(body.get("entries"), entry, 1000)
        if kind == "edit":
            action = body.get("action")
            if action not in ("favorite", "unfavorite", "forget", "remove") or any(
                (action == "unfavorite" and source != "favorites")
                or (action == "forget" and source != "recent")
                or (action == "remove" and source in LISTS)
                for source, _ in entries
            ):
                raise StickerError(
                    "This action is not allowed for the selected stickers."
                )
            return kind, dict(action=action, entries=entries)
        destination = body.get("destination")
        name, slug = None, None
        if destination is None:
            name, slug = title(body.get("title")), body.get("slug")
            if not isinstance(slug, str) or not SLUG.fullmatch(slug) or "__" in slug:
                raise StickerError(
                    "Use 1–64 English letters, digits or underscores for the link. Start with a letter and avoid double underscores."
                )
        else:
            destination = pack_id(destination)
        return kind, dict(
            entries=entries,
            destination=destination,
            title=name,
            slug=slug,
            install=body.get("install") is True,
        )
    raise StickerError("Unknown sticker action.")


@stickers_bp.errorhandler(StickerError)
async def invalid_action(error):
    return jsonify(error=str(error)), 400


@stickers_bp.errorhandler(MediaBusy)
@stickers_bp.errorhandler(MediaTooLarge)
@stickers_bp.errorhandler(OSError)
async def media_unavailable(_error):
    return jsonify(error="Sticker media is unavailable. Refresh and try again."), 503


@stickers_bp.get("/stickers")
async def page():
    workspace().prefetch()
    return await render_template("stickers/workspace.html.j2")


@stickers_bp.get("/api/stickers/packs")
async def packs():
    return jsonify(await workspace().summaries())


@stickers_bp.get("/api/stickers/items/<source>")
async def items(source):
    return jsonify(await workspace().items(source_id(source)))


@stickers_bp.post("/api/stickers/art/grid")
async def grid_art():
    service = workspace()
    entries = list_of(await request.get_json(silent=True), entry, 1000)
    dumps = current_app.json.dumps

    async def stream():
        try:
            async with aclosing(service.grid_artwork(entries)) as results:
                async for source, doc_id, image in results:
                    data = b64encode(image.data).decode() if image else None
                    yield dumps([f"{source}/{doc_id}", data]) + "\n"
        except AccountChanged:
            return

    response = current_app.response_class(stream(), mimetype="application/x-ndjson")
    response.headers["X-Accel-Buffering"] = "no"
    # The Telegram deadlines apply per lookup or download, not to the batch.
    response.timeout = None
    return response


@stickers_bp.get("/api/stickers/art/<source>/<sticker>/<mode>")
@stickers_bp.get(
    "/api/stickers/art/<source>/display", defaults={"sticker": None, "mode": "display"}
)
async def art(source, sticker, mode):
    if mode not in ("thumb", "full", "display"):
        abort(404)
    service = workspace()
    source, doc_id = (
        (pack_id(source), None)
        if sticker is None
        else (source_id(source), int(pack_id(sticker)))
    )
    item = await service.artwork(source, doc_id, mode)
    if not item:
        abort(404)
    response = await make_response(item.data)
    response.mimetype = item.mime_type
    return response


@stickers_bp.post("/api/stickers/operations")
async def create_operation():
    service = workspace()
    body = await request.get_json(silent=True)
    if not isinstance(body, dict):
        raise StickerError("Expected a sticker action.")
    kind, args = parse_operation(body)
    return jsonify(id=service.start(kind, args)), 202


@stickers_bp.get("/api/stickers/operations/<op_id>")
async def operation_status(op_id):
    op = workspace().operation(op_id)
    if not op:
        return jsonify(
            error="This sticker operation expired. Refresh to check Telegram’s current state."
        ), 404
    return jsonify(op.snapshot())


@stickers_bp.post("/api/stickers/operations/<op_id>/<action>")
async def update_operation(op_id, action):
    service = workspace()
    op = service.operation(op_id)
    if not op:
        return jsonify(
            error="This sticker operation expired. Refresh to check Telegram’s current state."
        ), 404
    if action == "cancel" and op.kind == "scan" and op.state == "running":
        op.cancel_requested = True
        op.message = "Canceling scan…"
        return jsonify(op.snapshot())
    if action == "retry" and op.retry and op.state != "running":
        next_id = service.start(op.kind, op.retry)
        op.retry = None
        return jsonify(id=next_id), 202
    follow_up = {"uninstall": "uninstall_original", "install": "can_install"}.get(
        action
    )
    if (
        follow_up
        and op.state != "running"
        and op.replacement
        and op.result.get(follow_up)
    ):
        next_id = service.start(
            "finish_copy",
            {"replacement": op.replacement, "uninstall": action == "uninstall"},
        )
        op.replacement = None
        del op.result[follow_up]
        return jsonify(id=next_id), 202
    raise StickerError("This follow-up is no longer available.")
