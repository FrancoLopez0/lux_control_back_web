from pydantic import BaseModel

class ControlValue(BaseModel):
    pwm:int