from pydantic import BaseModel

class LuxValue(BaseModel):
    lux:int
    time:str # Para debug lo dejo en str
