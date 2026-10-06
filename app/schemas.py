import re
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, Field, field_validator


class ConversionRequest(BaseModel):
    url: str = Field(max_length=2048)
    bitrate: int = Field(default=192)
    format: Literal["mp3", "mp4"] = "mp3"

    @field_validator("url")
    @classmethod
    def youtube_url(cls, value: str) -> str:
        try:
            parsed = urlsplit(value.strip())
            if (parsed.scheme not in {"https", "http"} or parsed.username
                    or parsed.password or parsed.port not in {None, 80, 443}):
                raise ValueError()
            host = parsed.hostname
            parts = parsed.path.strip("/").split("/")
            video_id = None
            if host == "youtu.be" and len(parts) == 1:
                video_id = parts[0]
            elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
                if parsed.path == "/watch":
                    video_id = parse_qs(parsed.query).get("v", [None])[0]
                elif len(parts) == 2 and parts[0] in {"shorts", "embed", "live"}:
                    video_id = parts[1]
            if not video_id or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
                raise ValueError()
        except ValueError:
            raise ValueError("Provide a valid YouTube video URL (playlists are not supported).") from None
        # Discard arbitrary query parameters and never fetch a user-supplied host.
        return f"https://www.youtube.com/watch?v={video_id}"

    @field_validator("bitrate")
    @classmethod
    def supported_bitrate(cls, value: int) -> int:
        if value not in {128, 192, 256, 320}:
            raise ValueError("Bitrate must be 128, 192, 256, or 320 kbps.")
        return value

