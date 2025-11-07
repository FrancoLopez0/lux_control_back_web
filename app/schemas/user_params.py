from pydantic import BaseModel

class UserParams(BaseModel):
    setPoint:int
    setPointFinal:int
    riseTime:int
    min:int
    max:int