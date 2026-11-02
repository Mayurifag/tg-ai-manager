import os
import re
from urllib.parse import quote

from quart import (
    Blueprint,
    abort,
    current_app,
    make_response,
    jsonify,
    redirect,
    request,
    send_from_directory,
    url_for,
)
from telethon.tl.types import DocumentAttributeFilename
from werkzeug.http import dump_options_header
from werkzeug.utils import secure_filename

from src.container import _get_tg_adapter, get_chat_interactor
from src.adapters.telegram.media import MediaBusy, MediaTooLarge

media_bp = Blueprint("media", __name__)


@media_bp.errorhandler(MediaBusy)
async def media_busy(_error):
    return jsonify({"error": "Media is busy; try again"}), 503


@media_bp.errorhandler(MediaTooLarge)
async def media_too_large(_error):
    return jsonify({"error": "Media exceeds the in-memory preview limit"}), 413


STATIC_DIR = os.path.join(os.getcwd(), "static")
CSS_DIR = os.path.join(STATIC_DIR, "css")
INLINE_MEDIA_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "video/webm",
}

os.makedirs(CSS_DIR, exist_ok=True)


async def _media_response(chat_id: int, msg_id: int, size_type: str):
    content = await get_chat_interactor().get_media(
        chat_id, msg_id, size_type=size_type
    )
    if not content:
        abort(404)
    if content.data is None:
        response = redirect(
            url_for("media.stream_document", chat_id=chat_id, msg_id=msg_id)
        )
    else:
        response = await make_response(content.data)
        response.mimetype = (
            content.mime_type
            if content.mime_type in INLINE_MEDIA_TYPES
            else "application/octet-stream"
        )
        if response.mimetype == "application/octet-stream":
            response.headers["Content-Disposition"] = "attachment"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@media_bp.route("/media/<int(signed=True):chat_id>/<int(signed=True):msg_id>")
async def get_message_media(chat_id: int, msg_id: int):
    return await _media_response(chat_id, msg_id, "preview")


@media_bp.route("/media/<int(signed=True):chat_id>/<int(signed=True):msg_id>/full")
async def get_message_media_full(chat_id: int, msg_id: int):
    return await _media_response(chat_id, msg_id, "full")


@media_bp.route("/media/stream/<int(signed=True):chat_id>/<int(signed=True):msg_id>")
async def stream_document(chat_id: int, msg_id: int):
    adapter = _get_tg_adapter()
    document = await adapter.get_stream_document(chat_id, msg_id)
    if (
        not document
        or not isinstance(getattr(document, "size", None), int)
        or document.size < 0
    ):
        abort(404)
    size = document.size
    start, end = 0, size
    raw_range = request.headers.get("Range")
    if raw_range:
        requested = request.range
        interval = (
            requested.range_for_length(size)
            if requested and requested.units == "bytes" and len(requested.ranges) == 1
            else None
        )
        if interval is None:
            response = await make_response("", 416)
            response.headers["Content-Range"] = f"bytes */{size}"
            response.headers["Cache-Control"] = "no-store"
            return response
        start, end = interval

    mime_type = getattr(document, "mime_type", "") or "application/octet-stream"
    response_mimetype = (
        mime_type
        if re.fullmatch(r"(?:video|audio)/[a-zA-Z0-9!#$&^_.+-]+", mime_type)
        else "application/octet-stream"
    )
    content_disposition = None
    if response_mimetype == "application/octet-stream":
        filename = next(
            (
                attr.file_name
                for attr in getattr(document, "attributes", [])
                if isinstance(attr, DocumentAttributeFilename)
            ),
            "file",
        )
        filename = (
            "".join(
                "_" if char in "/\\" else char
                for char in filename
                if 32 <= ord(char) != 127
            )
            or "file"
        )
        fallback = secure_filename(filename) or "file"
        content_disposition = (
            dump_options_header("attachment", {"filename": fallback})
            + "; filename*=UTF-8''"
            + quote(filename, safe="")
        )
    stream = adapter.stream_document(chat_id, msg_id, document, start, end - start)
    try:
        # Quart's make_response accepts async generators, but not arbitrary
        # async iterators such as the lease that owns our stream capacity.
        response = current_app.response_class(stream)
        response.status_code = 206 if raw_range else 200
        response.mimetype = response_mimetype
        if content_disposition:
            response.headers["Content-Disposition"] = content_disposition
        response.headers["Content-Length"] = str(end - start)
        response.headers["Accept-Ranges"] = "bytes"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if raw_range:
            response.headers["Content-Range"] = f"bytes {start}-{end - 1}/{size}"
        response.timeout = None
        return response
    except BaseException:
        await stream.aclose()
        raise


@media_bp.route("/media/avatar/<int(signed=True):chat_id>")
async def get_avatar(chat_id: int):
    interactor = get_chat_interactor()

    avatar = await interactor.get_chat_avatar(chat_id)
    if avatar:
        response = await make_response(avatar)
        response.mimetype = "image/jpeg"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return "", 404


@media_bp.route("/media/custom_emoji/<int(signed=True):doc_id>")
async def get_custom_emoji(doc_id: int):
    # Direct access to adapter needed as this method is specific to media mixin
    adapter = _get_tg_adapter()
    if not adapter:
        return "", 404

    content = await adapter.get_custom_emoji_media(doc_id)
    if content and content.data is not None:
        response = await make_response(content.data)
        response.mimetype = (
            content.mime_type
            if content.mime_type in INLINE_MEDIA_TYPES
            else "application/octet-stream"
        )
        if response.mimetype == "application/octet-stream":
            response.headers["Content-Disposition"] = "attachment"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return "", 404


# --- Static Asset Routes ---


@media_bp.route("/static/css/<path:filename>")
async def serve_css(filename):
    return await send_from_directory(CSS_DIR, filename)


@media_bp.route("/static/libs/<path:filename>")
async def serve_libs(filename):
    return await send_from_directory(os.path.join(STATIC_DIR, "libs"), filename)


@media_bp.route("/static/emoji/<path:filename>")
async def serve_static_emoji(filename):
    return await send_from_directory(os.path.join(STATIC_DIR, "emoji"), filename)
