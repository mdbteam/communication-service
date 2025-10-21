# app/main.py
from fastapi import FastAPI, Depends, HTTPException, status, WebSocket, WebSocketDisconnect
from typing import List
import pyodbc
import json
from dotenv import load_dotenv

load_dotenv()

from app.database import get_db_connection
from app.models import Message, UserInDB, ConversacionInfo, LastMessage
from app.auth_utils import get_current_user_from_cookie_or_token, get_current_user_from_token
from app.connection_manager import manager

app = FastAPI(
    title="Servicio de Comunicación - Chambee",
    description="Gestiona el chat en tiempo real y las notificaciones.",
    version="1.0.0"
)


@app.get("/", tags=["Status"])
def root():
    return {"message": "Communication Service funcionando 🚀"}


@app.get("/conversaciones", response_model=List[ConversacionInfo], tags=["Chat"])
def get_my_conversations(
        current_user: UserInDB = Depends(get_current_user_from_cookie_or_token),
        conn: pyodbc.Connection = Depends(get_db_connection)
):
    user_id = current_user.id_usuario
    cursor = conn.cursor()

    query = """
        SELECT
            c.id_conversacion,
            otro.id_usuario AS otro_usuario_id,
            CONCAT(otro.nombres, ' ', otro.primer_apellido) AS otro_usuario_nombre,
            otro.foto_url AS otro_usuario_foto_url,
            lm.contenido,
            lm.fecha_envio,
            (
                SELECT COUNT(*) 
                FROM Mensajes 
                WHERE id_conversacion = c.id_conversacion AND leido = 0 AND id_emisor != ?
            ) AS mensajes_no_leidos
        FROM Conversaciones c
        JOIN Usuarios otro ON otro.id_usuario = (
            CASE WHEN c.id_usuario_1 = ? THEN c.id_usuario_2 ELSE c.id_usuario_1 END
        )
        OUTER APPLY (
            SELECT TOP 1 contenido, fecha_envio
            FROM Mensajes
            WHERE id_conversacion = c.id_conversacion
            ORDER BY fecha_envio DESC
        ) AS lm
        WHERE c.id_usuario_1 = ? OR c.id_usuario_2 = ?
        ORDER BY lm.fecha_envio DESC;
    """

    cursor.execute(query, user_id, user_id, user_id, user_id)
    conversations_db = cursor.fetchall()
    cursor.close()

    conversations = []
    for row in conversations_db:
        last_message = None
        if row.contenido and row.fecha_envio:
            last_message = LastMessage(contenido=row.contenido, fecha_envio=row.fecha_envio)

        conversations.append(ConversacionInfo(
            id_conversacion=row.id_conversacion,
            otro_usuario_id=row.otro_usuario_id,
            otro_usuario_nombre=row.otro_usuario_nombre,
            otro_usuario_foto_url=row.otro_usuario_foto_url,
            mensajes_no_leidos=row.mensajes_no_leidos,
            ultimo_mensaje=last_message
        ))

    return conversations


@app.get("/conversaciones/{id_conversacion}/mensajes", response_model=List[Message], tags=["Chat"])
def get_message_history(
        id_conversacion: int,
        current_user: UserInDB = Depends(get_current_user_from_cookie_or_token),
        conn: pyodbc.Connection = Depends(get_db_connection)
):
    cursor = conn.cursor()
    cursor.execute("SELECT id_usuario_1, id_usuario_2 FROM Conversaciones WHERE id_conversacion = ?", id_conversacion)
    conversation = cursor.fetchone()
    if not conversation or current_user.id_usuario not in (conversation.id_usuario_1, conversation.id_usuario_2):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Conversación no encontrada o sin permiso.")

    cursor.execute(
        """
        SELECT 
            m.id_mensaje, m.id_conversacion, m.id_emisor, 
            CONCAT(u.nombres, ' ', u.primer_apellido) AS emisor,
            m.contenido, m.fecha_envio
        FROM Mensajes m
        JOIN Usuarios u ON m.id_emisor = u.id_usuario
        WHERE m.id_conversacion = ? 
        ORDER BY m.fecha_envio ASC
        """,
        id_conversacion
    )
    messages_db = cursor.fetchall()
    cursor.close()
    messages = [Message(**dict(zip([column[0] for column in row.cursor_description], row))) for row in messages_db]
    return messages


@app.post("/conversaciones/{id_conversacion}/leido", status_code=status.HTTP_204_NO_CONTENT, tags=["Chat"])
def mark_conversation_as_read(
        id_conversacion: int,
        current_user: UserInDB = Depends(get_current_user_from_cookie_or_token),
        conn: pyodbc.Connection = Depends(get_db_connection)
):
    user_id = current_user.id_usuario
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM Conversaciones WHERE id_conversacion = ? AND (id_usuario_1 = ? OR id_usuario_2 = ?)",
                   id_conversacion, user_id, user_id)
    if not cursor.fetchone():
        raise HTTPException(status_code=403, detail="No tienes permiso para acceder a esta conversación.")

    try:
        cursor.execute("UPDATE Mensajes SET leido = 1 WHERE id_conversacion = ? AND id_emisor != ?", id_conversacion,
                       user_id)
        conn.commit()
    except pyodbc.Error as e:
        conn.rollback();
        raise HTTPException(status_code=500, detail=f"Error al marcar mensajes como leídos: {e}")
    finally:
        cursor.close()
    return


@app.websocket("/ws/{id_conversacion}")
async def websocket_endpoint(
        websocket: WebSocket,
        id_conversacion: int,
        token: str,
        conn: pyodbc.Connection = Depends(get_db_connection)
):
    user = await get_current_user_from_token(token, conn)
    if not user:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION);
        return

    cursor = conn.cursor()
    cursor.execute("SELECT id_usuario_1, id_usuario_2 FROM Conversaciones WHERE id_conversacion = ?", id_conversacion)
    conversation = cursor.fetchone()
    if not conversation or user.id_usuario not in (conversation.id_usuario_1, conversation.id_usuario_2):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION);
        return

    await manager.connect(websocket, id_conversacion)

    try:
        while True:
            data = await websocket.receive_text()
            cursor.execute(
                "INSERT INTO Mensajes (id_conversacion, id_emisor, contenido) OUTPUT INSERTED.* VALUES (?, ?, ?)",
                id_conversacion, user.id_usuario, data)
            new_message_record = cursor.fetchone()
            conn.commit()

            message_to_broadcast = Message(
                id_mensaje=new_message_record.id_mensaje,
                id_conversacion=id_conversacion,
                id_emisor=user.id_usuario,
                emisor=f"{user.nombres} {user.primer_apellido}",
                contenido=data,
                fecha_envio=new_message_record.fecha_envio
            )

            await manager.broadcast(message_to_broadcast.model_dump_json(), id_conversacion)

    except WebSocketDisconnect:
        manager.disconnect(websocket, id_conversacion)
        cursor.close()
    except Exception as e:
        print(f"Error en el websocket: {e}")
        manager.disconnect(websocket, id_conversacion)
        cursor.close()