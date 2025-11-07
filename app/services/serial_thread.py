from schemas.control_value import ControlValue
from schemas.filter_value import FilterValue
from schemas.form_user import FormUser
from schemas.lux_value import LuxValue
from schemas.pid_value import PidValue
from schemas.sensors_value import SensorsValue
from schemas.sys_log import SysLog
from schemas.user_params import UserParams
import typing
import serial
from pydantic import ValidationError
import time

from typing import Dict, Any
from queue import Queue, Empty
from core.serial_events import SERIAL_STOP_EVENT

command_queue = Queue() 
data_queue = None

SERIAL_PORT = '/dev/ttyAMA0'
BAUD_RATE = 115200


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

def serial_worker(loop, data_queue:Queue):
    """
    Este es el HILO que corre por separado.
    Maneja TODA la comunicación serie.
    """
    print("Iniciando Hilo Guardián del Puerto Serie...")
    ser = None
    USER:UserParams = {
        "setPoint": 0,
        "setPointFinal":0,
        "riseTime":0,
        "min":0,
        "max":0,
    }

    PID:PidValue={
        "kp":0,
        "ki":0,
        "kd":0
    }

    try:
        # 1. Abrimos el puerto UNA SOLA VEZ
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.5)
        time.sleep(2) # Espera a que el dispositivo se estabilice
        print(f"Puerto {SERIAL_PORT} abierto exitosamente.")
        
        while not SERIAL_STOP_EVENT.is_set():
            # 2. Revisar si hay comandos entrantes desde FastAPI
            if not command_queue.empty():
                r = command_queue.get_nowait()
                if isinstance(r, tuple):
                    command, current_future = r
                    if command == "GET_USER":
                        if current_future:
                            loop.call_soon_threadsafe(current_future.set_result, USER)
                            continue
                command = r
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

                    loop.call_soon_threadsafe(data_queue.put_nowait, data)

                    if data["type"] == "lux_value":
                        continue

                    if data["type"] == "user_params":
                        USER = data['payload'] # Almaceno el estado actual del usuario
                        print(USER)

                    print(f"[Hilo Serie] Hardware response: {response}")
                    print(f"[Hilo Serie] FastAPI response: {data}")
                    
                except (IndexError, ValueError):
                    print(f"[Hilo Serie] Error al parsear respuesta: {response}")

            # 6. Esperar antes de la próxima lectura
            SERIAL_STOP_EVENT.wait(0.1)
            
        if ser and ser.is_open:
            ser.close()
            print("[Hilo Serie] Puerto serie cerrado.")

    except serial.SerialException as e:
        print(f"[Hilo Serie] ERROR: {e}")
        # Notificar al main thread que el puerto falló
        data = {"error": str(e)}
        loop.call_soon_threadsafe(data_queue.put_nowait, data)
        
    finally:
        if ser and ser.is_open:
            ser.close()
            print("[Hilo Serie] Puerto serie cerrado.")
