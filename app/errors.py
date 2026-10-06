"""Stable, safe API errors; upstream diagnostics stay in server logs."""

ERRORS = {
    "video_unavailable": (422, "This video is unavailable, private, or restricted."),
    "upstream_blocked": (502, "YouTube blocked this request or requires sign-in. Try another public video or retry later."),
    "format_unavailable": (422, "No downloadable video with audio is available for this link."),
    "duration_limit": (422, "The video is live, has an unknown duration, or exceeds the configured duration limit."),
    "size_limit": (413, "The video exceeds the server's download or output size limit. Try a shorter video."),
    "download_failed": (502, "YouTube download failed. Please retry later."),
    "encoding_failed": (500, "The server could not encode this video. See the server log using the request ID."),
    "invalid_audio": (500, "The converted audio could not be decoded. Please try again or report the job ID."),
    "invalid_output": (500, "The converted file failed validation. Please try another video."),
}


class ConversionFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(ERRORS[code][1])


def classify_download_error(message):
    message = str(message).lower()
    if any(term in message for term in ("sign in", "sign-in", "bot", "429", "403", "po token")):
        return "upstream_blocked"
    if any(term in message for term in ("unavailable", "private", "removed", "age-restricted", "not available in your country")):
        return "video_unavailable"
    if "requested format" in message or "no video formats" in message:
        return "format_unavailable"
    return "download_failed"
