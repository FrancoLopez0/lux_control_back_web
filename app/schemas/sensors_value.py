from pydantic import BaseModel

class SensorsValue(BaseModel):
    bh1750:int
    temt6000:float
    calib:float