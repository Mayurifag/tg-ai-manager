import os
from quart import Blueprint, send_from_directory, redirect, send_file
from src.container import get_chat_interactor

media_bp = Blueprint("media", __name__)

STATIC_DIR = os.path.join(os.getcwd(), "static")
IMAGES_DIR = os.path.join(os.getcwd(), "cache")
CSS_DIR = os.path.join(STATIC_DIR, "css")

os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(CSS_DIR, exist_ok=True)


@media_bp.route("/cache/<path:filename>")
async def serve_images(filename):
    return await send_from_directory(IMAGES_DIR, filename)


@media_bp.route("/media/<int(signed=True):chat_id>/<int(signed=True):msg_id>")
async def get_message_media(chat_id: int, msg_id: int):
    interactor = get_chat_interactor()

    public_path = await interactor.get_media_path(chat_id, msg_id)
    if public_path:
        return redirect(public_path)

    return "", 404


@media_bp.route("/media/avatar/<int(signed=True):chat_id>")
async def get_avatar(chat_id: int):
    interactor = get_chat_interactor()

    avatar_path = await interactor.get_chat_avatar(chat_id)
    if avatar_path and os.path.exists(avatar_path):
        return await send_file(avatar_path, mimetype="image/jpeg")

    # Return a 404 or a default placeholder if needed.
    # For now 404 so the frontend handles the error if desired,
    # though usually frontend just shows broken image.
    return "", 404


@media_bp.route("/static/css/<path:filename>")
async def serve_css(filename):
    return await send_from_directory(CSS_DIR, filename)
