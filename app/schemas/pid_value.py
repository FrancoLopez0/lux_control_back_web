from pydantic import BaseModel

class PidValue(BaseModel):
    kp:float
    ki:float
    kd:float