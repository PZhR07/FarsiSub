"""Describe audio streams by FFmpeg audio ordinal, not container stream index."""
def audio_streams(path):
    import av
    try:
        with av.open(str(path)) as container:
            streams = [{"track": ordinal, "language": stream.metadata.get("language", "und"),
                        "title": stream.metadata.get("title", ""),
                        "codec": stream.codec_context.name, "channels": stream.codec_context.channels}
                       for ordinal, stream in enumerate(container.streams.audio)]
    except Exception as exc:
        raise ValueError("فایل قابل خواندن نیست؛ مسیر و فرمت آن را بررسی کنید.") from exc
    if not streams:
        raise ValueError("این فایل مسیر صوتی ندارد.")
    return streams
