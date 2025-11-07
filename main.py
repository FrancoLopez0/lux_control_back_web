import asyncio
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
import requests
from starlette.concurrency import run_in_threadpool # Importante

# --- Configuración del Puerto Serie ---
SERIAL_PORT = 'COM13'
BAUD_RATE = 9600

# --- Colas de Comunicación ---
# 1. Cola de comandos: FastAPI -> Hilo Serie (Síncrona)
command_queue = Queue() 

# 2. Cola de datos: Hilo Serie -> FastAPI (Asíncrona)
# Esta se crea dentro del evento startup de FastAPI
data_queue = None

class UserParams(BaseModel):
    setPoint:int
    setPointFinal:int
    riseTime:int
    min:int
    max:int

class LuxValue(BaseModel):
    lux:int
    time:str # Para debug lo dejo en str

class FilterValue(BaseModel):
    r:float
    q:float
    alpha:float

class PidValue(BaseModel):
    kp:float
    ki:float
    kd:float

class SensorsValue(BaseModel):
    bh1750:int
    temt6000:float
    calib:float

class ControlValue(BaseModel):
    pwm:int

class SysLog(BaseModel):
    log:str
    time:str

MODELS = {
    "user_params": UserParams,
    "lux_value": LuxValue,
    "filter_value": FilterValue,
    "pid_value": PidValue,
    "sensors_value": SensorsValue,
    "control_value": ControlValue
}

def infer_and_tag_data(data: Dict[str, Any]) -> Dict[str, Any] | None:
    """
    Intenta validar el diccionario contra los modelos definidos.
    Si tiene éxito, retorna el diccionario con el campo 'type' añadido.
    """
    message = {}

    for type_name, model_class in MODELS.items():
        try:
            # Intentar crear una instancia del modelo con los datos
            # Esto valida tipos, campos requeridos y estructura.
            validated_model = model_class(**data)
            
            # Si tiene éxito, convertir el modelo validado a un diccionario,
            # añadir el campo 'type' e interrumpir el bucle.
            tagged_data = validated_model.model_dump() # Convierte el modelo Pydantic a dict
            message['payload'] = tagged_data
            message['type'] = type_name
            
            return message

        except ValidationError:
            # Si falla la validación, significa que los datos no coinciden con este modelo.
            # Simplemente continuamos al siguiente modelo en el diccionario MODELS.
            continue
            
    # Si el bucle termina sin un match
    print("Error: Los datos no coinciden con ninguna estructura de modelo conocida.")
    return None

# --- Administrador de WebSockets (igual que antes) ---
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

# --- El Hilo Guardián del Puerto Serie ---

def serial_worker(loop):
    """
    Este es el HILO que corre por separado.
    Maneja TODA la comunicación serie.
    """
    print("Iniciando Hilo Guardián del Puerto Serie...")
    ser = None
    try:
        # 1. Abrimos el puerto UNA SOLA VEZ
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.5)
        time.sleep(2) # Espera a que el dispositivo se estabilice
        print(f"Puerto {SERIAL_PORT} abierto exitosamente.")
        
        while True:
            # 2. Revisar si hay comandos entrantes desde FastAPI
            if not command_queue.empty():
                command = command_queue.get_nowait()
                print(f"[Hilo Serie] Recibido comando: {command}")
                ser.write(f"{command}\n".encode('utf-8'))
            
            # 3. Enviar el comando de sondeo periódico
            # ser.write(b"get_status\n")
            
            # 4. Leer la respuesta
            try:
                response = ser.readline().decode('utf-8').strip()
            except UnicodeDecodeError:
                print("Error en decodificacion")
                response = None
            
            # 5. Parsear y Poner datos en la cola de asyncio
            if response:
                # print(f"[Hilo Serie] Respuesta: {response}") # (Descomentar para debug)
                # Paso a Json los datos del microcontrolador
                try:
                    raw_data = response.split(",")
                    data = {}

                    for item in raw_data:
                        key, value = item.split(":")
                        key = key.replace(" ", "")

                        if(key == "time"):
                            data[key] = str(value).replace(" ", "")
                            continue
                        try:
                            data[key] = int(value)
                        except ValueError:
                            data[key] = float(value)
                            
                    data = infer_and_tag_data(data)

                    if data is None:
                        continue

                    # Comparo los datos con los dataclass para agregar un encabezado de tipo

                    # data={}
                    # data[key] = value
                    # data = "{" + response + "}"
                    # print(parts)
                    # Esta es la forma SEGURA de llamar a una función asyncio
                    # desde otro hilo (thread).
                    loop.call_soon_threadsafe(data_queue.put_nowait, data)

                    if data["type"] == "lux_value":
                        continue

                    print(f"[Hilo Serie] Hardware response: {response}")
                    print(f"[Hilo Serie] FastAPI response: {data}")
                    
                except (IndexError, ValueError):
                    print(f"[Hilo Serie] Error al parsear respuesta: {response}")

            # 6. Esperar antes de la próxima lectura
            time.sleep(0.1) # Frecuencia de sondeo

    except serial.SerialException as e:
        print(f"[Hilo Serie] ERROR: {e}")
        # Notificar al main thread que el puerto falló
        data = {"error": str(e)}
        loop.call_soon_threadsafe(data_queue.put_nowait, data)
        
    finally:
        if ser and ser.is_open:
            ser.close()
            print("[Hilo Serie] Puerto serie cerrado.")

# --- La Aplicación FastAPI ---

async def data_consumer_task():
    """
    Tarea Asíncrona que consume datos de la 'data_queue'
    y los envía a los clientes WebSocket.
    """
    print("Iniciando consumidor de datos (WebSocket)...")
    while True:
        data = await data_queue.get()
        await manager.broadcast_json(data)

@asynccontextmanager
async def startup_event(app: FastAPI):
    global data_queue
    loop = asyncio.get_event_loop()
    
    # Crear la cola de datos asíncrona
    data_queue = asyncio.Queue()
    
    # Iniciar el Hilo Guardián
    threading.Thread(
        target=serial_worker, 
        args=(loop,), 
        daemon=True # Muere si el proceso principal muere
    ).start()
    
    # Iniciar la tarea que envía datos a los WebSockets
    asyncio.create_task(data_consumer_task())
    yield

app = FastAPI(lifespan=startup_event)

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
        return {"status": "error", "message": "Comando vacío"}
        
    print(f"[FastAPI] Poniendo comando en cola: {command}")
    
    # Usamos run_in_threadpool para llamar a 'put' (que es bloqueante)
    # de forma segura sin bloquear el bucle de eventos de FastAPI.
    await run_in_threadpool(command_queue.put, command)
    
    return {"status": "comando_enviado"}

@app.post("/user_params")
async def user_params(data: UserParams):
    """
    Endpoint HTTP para enviar un comando al Hilo Guardián.
    """
    if not data:
        return {"status": "error", "message": "Comando vacío"}
            
    # Usamos run_in_threadpool para llamar a 'put' (que es bloqueante)
    # de forma segura sin bloquear el bucle de eventos de FastAPI.
    await run_in_threadpool(command_queue.put, f"set set_point {data.setPoint}")
    await run_in_threadpool(command_queue.put, f"set rise_time {data.riseTime}")
    await run_in_threadpool(command_queue.put, f"set set_point_f {data.setPointFinal}")
    await run_in_threadpool(command_queue.put, f"set min {data.min}")
    await run_in_threadpool(command_queue.put, f"set max {data.max}")
    
    return {"status": "comando_enviado"}

@app.get("/user_params")
async def user_params():
    """
    Endpoint HTTP para enviar un comando al Hilo Guardián.
    """

    # Usamos run_in_threadpool para llamar a 'put' (que es bloqueante)
    # de forma segura sin bloquear el bucle de eventos de FastAPI.
    await run_in_threadpool(command_queue.put, f"get user_params")
    
    return {"status": "comando_enviado"}

@app.post("/pid_control")
async def pid_control(data: PidValue):
    """
    Endpoint HTTP para enviar un comando al Hilo Guardián.
    """
    if not data:
        return {"status": "error", "message": "Comando vacío"}
            
    # Usamos run_in_threadpool para llamar a 'put' (que es bloqueante)
    # de forma segura sin bloquear el bucle de eventos de FastAPI.
    await run_in_threadpool(command_queue.put, f"set pid_kp {data.kp}")
    await run_in_threadpool(command_queue.put, f"set pid_ki {data.ki}")
    await run_in_threadpool(command_queue.put, f"set pid_kd {data.kd}")
    
    return {"status": "comando_enviado"}

@app.get("/pid_control")
async def pid_control():
    """
    Endpoint HTTP para enviar un comando al Hilo Guardián.
    """

    # Usamos run_in_threadpool para llamar a 'put' (que es bloqueante)
    # de forma segura sin bloquear el bucle de eventos de FastAPI.
    await run_in_threadpool(command_queue.put, f"get pid_params")
    
    return {"status": "comando_enviado"}

@app.post("/filter_value")
async def filter_value(data: FilterValue):
    """
    Endpoint HTTP para enviar un comando al Hilo Guardián.
    """
    if not data:
        return {"status": "error", "message": "Comando vacío"}
            
    # Usamos run_in_threadpool para llamar a 'put' (que es bloqueante)
    # de forma segura sin bloquear el bucle de eventos de FastAPI.
    await run_in_threadpool(command_queue.put, f"set kalman_R {data.r}")
    await run_in_threadpool(command_queue.put, f"set kalman_Q {data.q}")
    await run_in_threadpool(command_queue.put, f"set alpha {data.alpha}")
    
    return {"status": "comando_enviado"}

@app.get("/filter_value")
async def filter_value():
    """
    Endpoint HTTP para enviar un comando al Hilo Guardián.
    """

    # Usamos run_in_threadpool para llamar a 'put' (que es bloqueante)
    # de forma segura sin bloquear el bucle de eventos de FastAPI.
    await run_in_threadpool(command_queue.put, f"get filter_params")
    
    return {"status": "comando_enviado"}
