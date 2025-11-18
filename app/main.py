# communication-service/app/main.py
from fastapi import FastAPI, Depends, HTTPException, status, WebSocket, WebSocketDisconnect, APIRouter, Query
from typing import List, Optional
import pyodbc
import json
from dotenv import load_dotenv

load_dotenv(override=True)

from app.database import get_db_connection
from app.models import Message, UserInDB, ConversacionInfo, LastMessage, SendMessage
from app.auth_utils import get_current_user_from_cookie_or_token, get_current_user_from_token
from app.connection_manager import manager

app = FastAPI(
    title="Servicio de Comunicación - Chambee",
    description="Gestiona el chat en tiempo real y las notificaciones.",
    version="1.0.0"
)

# --- CONFIGURACIÓN CORS ---
origins = [
    "http://localhost",
    "http://localhost:8081",
    "https://auth-service-1-8301.onrender.com",
    "*",  # solo para desarrollo
]


app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,             # Permite enviar credenciales (cookies, auth headers)
    allow_methods=["*"],                # Permite todos los métodos HTTP
    allow_headers=["*"],                # Permite todas las cabeceras
)
# --- CONFIGURACIÓN CORS ---


# Creamos un router con el prefijo /api
router = APIRouter(prefix="/api")


@app.get("/", tags=["Status"])
def root():
    return {"message": "Communication Service funcionando 🚀"}


# --- ENDPOINT DE BANDEJA DE ENTRADA (CON PREFIJO /api) ---
@router.get("/conversaciones", response_model=List[ConversacionInfo], tags=["Chat"])
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

    try:
        cursor.execute(query, user_id, user_id, user_id, user_id)
        conversations_db = cursor.fetchall()
    except pyodbc.Error as e:
        raise HTTPException(status_code=500, detail=f"Error BBDD: {e}")
    finally:
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


# --- ENDPOINT DE HISTORIAL (CRÍTICO - REQUERIMIENTO 3.4) ---
@router.get("/chat/history/{id_otro_usuario}", response_model=List[Message], tags=["Chat"])
def get_chat_history_with_user(
        id_otro_usuario: int,
        current_user: UserInDB = Depends(get_current_user_from_cookie_or_token),
        conn: pyodbc.Connection = Depends(get_db_connection)
):
    """Obtiene el historial de mensajes entre el usuario actual y otro usuario."""
    user_id = current_user.id_usuario
    cursor = conn.cursor()

    # 1. Buscar la conversación
    cursor.execute(
        "SELECT id_conversacion FROM Conversaciones WHERE (id_usuario_1 = ? AND id_usuario_2 = ?) OR (id_usuario_1 = ? AND id_usuario_2 = ?)",
        user_id, id_otro_usuario, id_otro_usuario, user_id
    )
    conversation = cursor.fetchone()

    if not conversation:
        cursor.close()
        return []  # No hay historial, devuelve lista vacía

    id_conversacion = conversation.id_conversacion

    # 2. Obtener los mensajes
    try:
        cursor.execute(
            """
            SELECT m.id_mensaje, m.id_conversacion, m.id_emisor, 
                   CONCAT(u.nombres, ' ', u.primer_apellido) AS emisor,
                   m.contenido, m.fecha_envio
            FROM Mensajes m
            JOIN Usuarios u ON m.id_emisor = u.id_usuario
            WHERE m.id_conversacion = ? ORDER BY m.fecha_envio ASC
            """,
            id_conversacion
        )
        messages_db = cursor.fetchall()
        messages = [Message(**dict(zip([column[0] for column in row.cursor_description], row))) for row in messages_db]
        return messages
    except pyodbc.Error as e:
        raise HTTPException(status_code=500, detail=f"Error BBDD: {e}")
    finally:
        cursor.close()


# --- ENDPOINT PARA MARCAR LEÍDO (CON PREFIJO /api) ---
@router.post("/conversaciones/{id_conversacion}/leido", status_code=status.HTTP_204_NO_CONTENT, tags=["Chat"])
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


# Incluimos todas las rutas HTTP en la app con el prefijo /api
app.include_router(router)


# --- ENDPOINT WEBSOCKET (REFACTORIZADO) ---
# (Va sin el prefijo /api)
@app.websocket("/ws")
async def websocket_endpoint(
        websocket: WebSocket,
        token: str = Query(...),  # El token se pasa como query param
        conn: pyodbc.Connection = Depends(get_db_connection)
):
    """Maneja la conexión WebSocket de un usuario."""
    # 1. Autenticar al usuario
    user = await get_current_user_from_token(token, conn)
    if not user:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Token inválido")
        return

    # 2. Conectar al usuario al gestor
    user_id_emisor = user.id_usuario
    await manager.connect(websocket, user_id_emisor)

    cursor = conn.cursor()  # Abrimos cursor para reutilizar
    try:
        # 3. Escuchar mensajes
        while True:
            data_json = await websocket.receive_json()

            # Validamos el formato { "id_destinatario": ..., "contenido": ... } (Req 3.3)
            try:
                message_in = SendMessage(**data_json)
            except Exception:
                await websocket.send_text(json.dumps(
                    {"error": "Formato de mensaje incorrecto. Se esperaba {'id_destinatario': int, 'contenido': str}"}))
                continue

            id_destinatario = message_in.id_destinatario
            contenido = message_in.contenido

            # 4. Buscar o crear la conversación
            id_usuario_1 = min(user_id_emisor, id_destinatario)
            id_usuario_2 = max(user_id_emisor, id_destinatario)

            cursor.execute("SELECT id_conversacion FROM Conversaciones WHERE id_usuario_1 = ? AND id_usuario_2 = ?",
                           id_usuario_1, id_usuario_2)
            conversation = cursor.fetchone()

            if not conversation:
                # Si no existe, la creamos (Aunque el 'calendar-service' ya debería haberlo hecho)
                cursor.execute(
                    "INSERT INTO Conversaciones (id_usuario_1, id_usuario_2) OUTPUT INSERTED.id_conversacion VALUES (?, ?)",
                    id_usuario_1, id_usuario_2)
                id_conversacion = cursor.fetchone()[0]
            else:
                id_conversacion = conversation.id_conversacion

            # 5. Guardar el mensaje en la BBDD
            cursor.execute(
                "INSERT INTO Mensajes (id_conversacion, id_emisor, contenido) OUTPUT INSERTED.* VALUES (?, ?, ?)",
                id_conversacion, user_id_emisor, contenido)
            new_message_record = cursor.fetchone()
            conn.commit()

            # 6. Crear el objeto de mensaje completo (Formato 3.2)
            message_to_send = Message(
                id_mensaje=new_message_record.id_mensaje,
                id_conversacion=id_conversacion,
                id_emisor=user_id_emisor,
                emisor=f"{user.nombres} {user.primer_apellido}",
                contenido=contenido,
                fecha_envio=new_message_record.fecha_envio
            )
            message_json = message_to_send.model_dump_json()

            # 7. Enviar al destinatario (si está conectado)
            await manager.send_personal_message(message_json, id_destinatario)

            # 8. Enviarse el mensaje a sí mismo (para confirmación de "enviado")
            await websocket.send_text(message_json)

    except WebSocketDisconnect:
        manager.disconnect(user_id_emisor)
        if cursor: cursor.close()
    except Exception as e:
        print(f"Error en WebSocket: {e}")
        manager.disconnect(user_id_emisor)
        if cursor: cursor.close()
