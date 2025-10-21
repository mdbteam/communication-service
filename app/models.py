# app/models.py
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime

class Message(BaseModel):
    id_mensaje: int
    id_conversacion: int
    id_emisor: int
    emisor: str
    contenido: str
    fecha_envio: datetime

class UserInDB(BaseModel):
    id_usuario: int
    id_rol: int
    estado: str
    nombres: str
    primer_apellido: str
    correo: str

class LastMessage(BaseModel):
    contenido: str
    fecha_envio: datetime

class ConversacionInfo(BaseModel):
    id_conversacion: int
    otro_usuario_id: int
    otro_usuario_nombre: str
    otro_usuario_foto_url: str
    mensajes_no_leidos: int
    ultimo_mensaje: Optional[LastMessage] = None