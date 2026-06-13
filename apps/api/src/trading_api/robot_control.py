"""Thin robot command placeholder for API role/contract wiring."""

from __future__ import annotations

from dataclasses import dataclass

from trading_api.schemas import ApiRole, RobotCommand, RobotCommandResponse


@dataclass(slots=True)
class RobotControlState:
    state: str = "stopped"

    def start(self, *, role: ApiRole) -> RobotCommandResponse:
        self.state = "start_requested"
        return RobotCommandResponse(
            accepted=True,
            command=RobotCommand.START,
            requested_by_role=role,
            status=self.state,
            message="Robot start command accepted by BFF placeholder",
        )

    def stop(self, *, role: ApiRole) -> RobotCommandResponse:
        self.state = "stop_requested"
        return RobotCommandResponse(
            accepted=True,
            command=RobotCommand.STOP,
            requested_by_role=role,
            status=self.state,
            message="Robot stop command accepted by BFF placeholder",
        )
