import asyncio

from telethon import errors

from src.infrastructure.telegram_queue import WriteQueueFull

FILE_REFERENCE_ERRORS = (
    errors.FileReferenceExpiredError,
    errors.FileReferenceEmptyError,
    errors.FileReferenceInvalidError,
    errors.FilerefUpgradeNeededError,
)


class StickerError(Exception):
    pass


class Uncertain(StickerError):
    pass


def rpc_error(error):
    code = getattr(error, "message", "")
    if isinstance(
        error, (errors.StickersTooMuchError, errors.StickerpackStickersTooMuchError)
    ) or code in {
        "STICKERPACK_STICKERS_TOO_MUCH",
        "STICKERS_TOO_MUCH",
        "STICKERSETS_TOO_MUCH",
    }:
        return StickerError("Telegram’s pack or sticker limit was reached.")
    if (
        isinstance(error, errors.PackShortNameOccupiedError)
        or code == "PACK_SHORT_NAME_OCCUPIED"
    ):
        return StickerError("This Telegram link is taken. Choose another short name.")
    if isinstance(error, FILE_REFERENCE_ERRORS):
        return StickerError(
            "The sticker reference expired. Refresh the pack and try again."
        )
    return StickerError(f"Telegram rejected the request ({type(error).__name__}).")


class StickerRPC:
    def __init__(self, client, queue, check):
        self.client = client
        self.queue = queue
        self.check = check

    async def read(self, request):
        self.check()
        try:
            async with asyncio.timeout(20):
                return await self.client(request)
        except (errors.FloodWaitError, errors.FloodPremiumWaitError) as error:
            raise StickerError(
                f"Telegram requires a wait of {error.seconds}s. Try again after the wait."
            ) from error
        except errors.RPCError as error:
            raise rpc_error(error) from error
        except (OSError, ValueError) as error:
            raise StickerError("Could not refresh from Telegram. Try again.") from error
        finally:
            self.check()

    async def write(self, op, request, *, reconcile=None, guard=None):
        """Queue one mutation. Reconcile ambiguity; never replay it."""
        while True:
            self.check()
            try:
                result = await self._write_attempt(request, guard)
                break
            except (errors.FloodWaitError, errors.FloodPremiumWaitError) as error:
                previous = op.message
                op.message = (
                    f"Telegram FLOOD_WAIT: waiting {error.seconds}s before continuing."
                )
                # Telegram rejected this request. Wait in the operation task,
                # then rejoin the queue and recheck the guard before sending.
                await asyncio.sleep(max(1, error.seconds))
                op.message = previous
            except WriteQueueFull as error:
                raise StickerError(str(error)) from error
            except errors.RPCError as error:
                if isinstance(error, FILE_REFERENCE_ERRORS):
                    raise
                raise rpc_error(error) from error
        if result is not None:
            return result
        try:
            verified = reconcile and await reconcile()
            if verified:
                return verified
        except StickerError, OSError:
            pass
        raise Uncertain(
            "Telegram’s response was lost and the change could not be verified. It was not retried."
        )

    async def _write_attempt(self, request, guard):
        future = asyncio.get_running_loop().create_future()

        async def execute():
            try:
                if future.cancelled():
                    return
                self.check()
                if guard:
                    await guard()
                if future.cancelled():
                    return
                self.check()
                try:
                    result = await self.client.send_once(request)
                except OSError, errors.BadMessageError, errors.TypeNotFoundError:
                    result = None
                except errors.RPCError as error:
                    if abs(error.code or 0) < 500 and not isinstance(
                        error, errors.TimedOutError
                    ):
                        raise
                    result = None
                if result is False:
                    raise StickerError("Telegram declined the change.")
                if not future.done():
                    future.set_result(result)
            except Exception as error:
                if not future.done():
                    future.set_exception(error)
            except asyncio.CancelledError:
                if not future.done():
                    future.set_result(None)
                if asyncio.current_task().cancelling():
                    raise

        await self.queue.enqueue(execute)
        return await future

    async def upload_bytes(self, op, data, filename):
        while True:
            try:
                async with asyncio.timeout(25):
                    return await self.client.upload_file(data, file_name=filename)
            except (errors.FloodWaitError, errors.FloodPremiumWaitError) as error:
                previous = op.message
                op.message = (
                    f"Telegram FLOOD_WAIT: waiting {error.seconds}s before uploading."
                )
                await asyncio.sleep(max(1, error.seconds))
                op.message = previous
            except (OSError, errors.RPCError, ValueError) as error:
                raise StickerError(
                    "Could not upload the original sticker bytes."
                ) from error
