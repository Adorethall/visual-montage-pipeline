from __future__ import annotations


async def marlin_caption(video_url: str):
    from worker_stubs.marlin import MarlinCaptionInput, marlin_caption_stub
    return await marlin_caption_stub.aio_run(input=MarlinCaptionInput(video_url=video_url))


async def marlin_find(video_url: str, event: str):
    from worker_stubs.marlin import MarlinFindInput, marlin_find_stub
    return await marlin_find_stub.aio_run(input=MarlinFindInput(video_url=video_url, event=event))


async def synthesize_product_voiceover(text: str, voice: str):
    from worker_stubs.omnivoice import OmniVoiceVoiceDesignInput, omnivoice_voice_design_stub
    try:
        return await omnivoice_voice_design_stub.aio_run(
            input=OmniVoiceVoiceDesignInput(text=text, voice=voice, trim_output=True)
        )
    except Exception:
        from worker_stubs.voxcpm import VoxCPMVoiceDesignTaskInput, voxcpm_voice_design_stub
        return await voxcpm_voice_design_stub.aio_run(
            input=VoxCPMVoiceDesignTaskInput(text=text, voice=voice, retry_badcase=True)
        )

