from typing import Literal
from pydantic import BaseModel


class Leverage(BaseModel):
    type: Literal["cross", "isolated"]
    value: int


class CumFunding(BaseModel):
    allTime: str
    sinceOpen: str
    sinceChange: str


class HLPosition(BaseModel):
    coin: str
    szi: str                          # signed size; negative = short
    leverage: Leverage
    entryPx: str
    positionValue: str
    unrealizedPnl: str
    returnOnEquity: str
    liquidationPx: str | None         # null when over-collateralized
    marginUsed: str
    maxLeverage: int                  # symbol's ceiling, not position's current
    cumFunding: CumFunding


class HLAssetPosition(BaseModel):
    type: Literal["oneWay", "hedged"]
    position: HLPosition


class HLMarginSummary(BaseModel):
    accountValue: str
    totalNtlPos: str
    totalRawUsd: str
    totalMarginUsed: str


class HLClearinghouseState(BaseModel):
    marginSummary: HLMarginSummary
    crossMarginSummary: HLMarginSummary
    crossMaintenanceMarginUsed: str
    withdrawable: str
    assetPositions: list[HLAssetPosition]
    time: int