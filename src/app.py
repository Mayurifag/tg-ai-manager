from quart import Quart, render_template
from src.container import get_chat_service

app = Quart(__name__)

@app.before_serving
async def startup():
    service = get_chat_service()
    await service.initialize()

@app.after_serving
async def shutdown():
    service = get_chat_service()
    await service.shutdown()

@app.route("/")
async def index():
    service = get_chat_service()
    chats = await service.get_recent_chats()
    return await render_template("index.html", chats=chats)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
