import asyncio
import json
import os
from fastapi import APIRouter, WebSocket
from fastapi.websockets import WebSocketDisconnect
import websockets

from app.core.config import logger, DEEPGRAM_API_KEY

router = APIRouter(prefix="/api", tags=["voice"])


@router.websocket("/voice/stream")
async def ws_voice_stream(ws: WebSocket):
    await ws.accept()
    logger.info("Voice WS: client connected")
    if not DEEPGRAM_API_KEY:
        await ws.send_text(json.dumps({"error": "Deepgram not configured"}))
        await ws.close()
        return

    qp = "model=nova-2&smart_format=true&punctuate=true&language=en-US&encoding=opus&vad_events=true&endpointing=true"
    dg_url = f"wss://api.deepgram.com/v1/listen?{qp}"

    async def _pump_client_to_dg(dg_conn):
        try:
            while True:
                msg = await ws.receive()
                t = msg.get("type")
                if t == "websocket.disconnect":
                    logger.info("Voice WS: client sent disconnect")
                    try:
                        await dg_conn.close()
                    except Exception:
                        pass
                    break
                data = msg.get("bytes")
                if data is not None:
                    await dg_conn.send(data)
                    continue
                txt = msg.get("text")
                if txt is not None:
                    await dg_conn.send(txt)
        except WebSocketDisconnect:
            logger.info("Voice WS: client disconnected")
            try:
                await dg_conn.close()
            except Exception:
                pass
        except Exception as ex:
            logger.warning(f"Voice WS: error receiving from client: {ex}")
            try:
                await dg_conn.close()
            except Exception:
                pass

    async def _pump_dg_to_client(dg_conn):
        try:
            async for message in dg_conn:
                if isinstance(message, bytes):
                    await ws.send_bytes(message)
                else:
                    await ws.send_text(message)
        except Exception as ex:
            logger.warning(f"Voice WS: error sending to client: {ex}")
            try:
                await ws.close()
            except Exception:
                pass

    try:
        async with websockets.connect(
            dg_url,
            extra_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
            max_size=None,
            open_timeout=20,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
        ) as dg_conn:
            logger.info("Voice WS: connected to Deepgram")
            # Notify client that upstream is ready so it can start streaming audio
            try:
                await ws.send_text(json.dumps({"ready": True}))
            except Exception:
                pass
            await asyncio.gather(_pump_client_to_dg(dg_conn), _pump_dg_to_client(dg_conn))
    except Exception as ex:
        logger.exception(f"Deepgram proxy error: {ex}")
        try:
            await ws.send_text(json.dumps({"error": str(ex)}))
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass
        logger.info("Voice WS: closed")