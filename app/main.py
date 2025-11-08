import asyncio
from asyncio import Future
import serial
import threading
import time
from pydantic import dataclasses
from dataclasses import dataclass
from pydantic import BaseModel, ValidationError
from typing import Dict, Any
from queue import Queue, Empty
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from contextlib import asynccontextmanager
from schemas.control_value import ControlValue
from schemas.filter_value import FilterValue
from schemas.form_user import FormUser
from schemas.lux_value import LuxValue
from schemas.pid_value import PidValue
from schemas.sensors_value import SensorsValue
from schemas.sys_log import SysLog
from schemas.user_params import UserParams
from core.serial_events import SERIAL_STOP_EVENT

import requests
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool # Importante

from services.serial_thread import serial_worker, command_queue

USER:UserParams|None = None
PID:PidValue|None = None
FILTER:FilterValue|None = None

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        await run_in_threadpool(command_queue.put, f"get user_params")
        await run_in_threadpool(command_queue.put, f"get filter_params")
        await run_in_threadpool(command_queue.put, f"get pid_params")

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast_json(self, data: dict):
        for connection in self.active_connections:
            await connection.send_json(data)

manager = ConnectionManager()

async def data_consumer_task(data_queue: Queue):
    """
    Tarea Asíncrona que consume datos de la 'data_queue'
    y los envía a los clientes WebSocket.
    """
    global USER
    global PID
    global FILTER
    print("Iniciando consumidor de datos (WebSocket)...")
    while True:
        data = await data_queue.get()

        if data["type"] == "user_params":
            USER = UserParams(**data["payload"])
        if data["type"] == "pid_value":
            PID = PidValue(**data["payload"])
        if data["type"] == "filter_value":
            FILTER = FilterValue(**data["payload"])

        await manager.broadcast_json(data)

@asynccontextmanager
async def startup_event(app: FastAPI):
    loop = asyncio.get_event_loop()
    
    # Crear la cola de datos asíncrona
    data_queue = asyncio.Queue()
    
    # Iniciar el Hilo Guardián
    serial_thread = threading.Thread(
        target=serial_worker, 
        args=(loop, data_queue), 
        daemon=True # Muere si el proceso principal muere
    )

    serial_thread.start()
    
    # Iniciar la tarea que envía datos a los WebSockets
    # asyncio.create_task(data_consumer_task(data_queue))
    asyncio.create_task(data_consumer_task(data_queue))

    future = Future()

    command_item = ("GET_USER", future) 

    await run_in_threadpool(command_queue.put, command_item)

    yield

    print("Deteniendo tarea guardiana....")

    SERIAL_STOP_EVENT.set()
    
app = FastAPI(lifespan=startup_event)

origins = [
    "http://localhost:3000",
    "http://192.168.1.47:3000",
    "*" 
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,        # Define qué orígenes están permitidos
    allow_credentials=True,       # Permite cookies y encabezados de autenticación
    allow_methods=["*"],          # **Permite todos los métodos**, incluyendo OPTIONS
    allow_headers=["*"],          # Permite todos los encabezados
)

@app.websocket("/ws/status")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text() # Solo para mantener viva la conexión
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.post("/api/command")
async def send_command(command: str):
    """
    Endpoint HTTP para enviar un comando al Hilo Guardián.
    """
    if not command:
        return {"status": "error", "message": "a vacío"}
        
    print(f"[FastAPI] Poniendo comando en cola: {command}")
    
    # Usamos run_in_threadpool para llamar a 'put' (que es bloqueante)
    # de forma segura sin bloquear el bucle de eventos de FastAPI.
    await run_in_threadpool(command_queue.put, command)
    
    return {"status": "comando_enviado"}

cmd = {
    "setPoint":"set_point",
    "riseTime":"rise_time",
    "setPointFinal":"set_point_f",
    "min":"min",
    "max":"max",
    "kp":"pid_kp",
    "ki":"pid_ki",
    "kd":"pid_kd"
    }

async def set_cmd(key:str, arg):
    await run_in_threadpool(command_queue.put, f"set {cmd[key]} {arg}")

async def set_(new, prev):
    if new != prev and prev != None:
        for r in cmd.keys():
            if r in new.model_fields.keys():
                if getattr(new, r) != getattr(prev,r):
                    await set_cmd(r, getattr(new, r))

async def set_user_params(data: UserParams):
    global USER
    await set_(data, USER)
    return {
        "status":"ok",
        "message": data
    }
    
async def set_pid_params(data: PidValue):
    global PID
    await set_(data, PID)
    return {
        "status":"ok",
        "message": data
    }

async def set_filter_params(data: FilterValue):
    global FILTER
    await set_(data, FILTER)
    return {
        "status":"ok",
        "message": data
    }

@app.post("/user_params")
async def user_params(data: UserParams):
    """
    Endpoint HTTP para enviar un comando al Hilo Guardián.
    """
    if not data:
        return {"status": "error", "message": "Comando vacío"}
            
    # Usamos run_in_threadpool para llamar a 'put' (que es bloqueante)
    # de forma segura sin bloquear el bucle de eventos de FastAPI.
    return await set_user_params(data)

@app.get("/user_params")
async def user_params():
    """
    Endpoint HTTP para enviar un comando al Hilo Guardián.
    """

    future = Future()

    command_item = ("GET_USER", future)

    await run_in_threadpool(command_queue.put, command_item)
    
    try:
        response_data = await asyncio.wait_for(future, timeout=2.0)

        return {"status": "ok", "data": response_data}

    except asyncio.TimeoutError:
        return {"status": "error", "message": "El Hilo Guardián no respondió a tiempo."}
    except Exception as e:
        return {"status": "error", "message": f"Error inesperado: {e}"}

@app.post("/pid_params")
async def pid_params(data: PidValue):
    """
    Endpoint HTTP para enviar un comando al Hilo Guardián.
    """
    if not data:
        return {"status": "error", "message": "Comando vacío"}
            
    # Usamos run_in_threadpool para llamar a 'put' (que es bloqueante)
    # de forma segura sin bloquear el bucle de eventos de FastAPI.
    return await set_pid_params(data)

@app.get("/pid_params")
async def pid_control():
    """
    Endpoint HTTP para enviar un comando al Hilo Guardián. 
    """

    # Usamos run_in_threadpool para llamar a 'put' (que es bloqueante)
    # de forma segura sin bloquear el bucle de eventos de FastAPI.
    await run_in_threadpool(command_queue.put, f"get pid_params")
    
    return {"status": "comando_enviado"}

@app.post("/filter_params")
async def filter_value(data: FilterValue):
    """
    Endpoint HTTP para enviar un comando al Hilo Guardián.
    """
    if not data:
        return {"status": "error", "message": "Comando vacío"}
            
    # Usamos run_in_threadpool para llamar a 'put' (que es bloqueante)
    # de forma segura sin bloquear el bucle de eventos de FastAPI.
    return await set_filter_params(data)

@app.get("/filter_params")
async def filter_value():
    """
    Endpoint HTTP para enviar un comando al Hilo Guardián.
    """

    # Usamos run_in_threadpool para llamar a 'put' (que es bloqueante)
    # de forma segura sin bloquear el bucle de eventos de FastAPI.
    await run_in_threadpool(command_queue.put, f"get filter_params")
    
    return {"status": "comando_enviado"}

@app.post("/params")
async def set_params(r: FormUser):
    
    # await set_pid_params(r.pidValue)
    return await set_user_params(r.userParams)
    # await set_filter_value(r.filterValue)

    