# communication-service/app/connection_manager.py
from fastapi import WebSocket
from typing import Dict, Optional
import json

class ConnectionManager:
    def __init__(self):
        # Mapea un id_usuario a su conexión WebSocket activa
        self.active_connections: Dict[int, WebSocket] = {}

    async def connect(self, websocket: WebSocket, user_id: int):
        await websocket.accept()
        self.active_connections[user_id] = websocket
        print(f"DEBUG: Usuario {user_id} conectado. Total conexiones: {len(self.active_connections)}")

    def disconnect(self, user_id: int):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
            print(f"DEBUG: Usuario {user_id} desconectado. Total conexiones: {len(self.active_connections)}")

    async def send_personal_message(self, message: str, user_id: int):
        """Envía un mensaje a un usuario específico si está conectado."""
        websocket = self.active_connections.get(user_id)
        if websocket:
            await websocket.send_text(message)
            print(f"DEBUG: Mensaje enviado a {user_id}")
            return True
        else:
            print(f"DEBUG: Usuario {user_id} no conectado. Mensaje no enviado.")
            return False

manager = ConnectionManager()