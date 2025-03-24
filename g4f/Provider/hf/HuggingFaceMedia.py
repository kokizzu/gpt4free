from __future__ import annotations

import time
import asyncio
import random
import requests

from ...providers.types import Messages
from ...requests import StreamSession, raise_for_status
from ...errors import ModelNotSupportedError
from ...providers.helper import format_image_prompt
from ...providers.base_provider import AsyncGeneratorProvider, ProviderModelMixin
from ...providers.response import ProviderInfo, ImageResponse, VideoResponse, Reasoning
from ...image.copy_images import save_response_media
from ...image import use_aspect_ratio
from ... import debug

class HuggingFaceMedia(AsyncGeneratorProvider, ProviderModelMixin):
    label = "HuggingFace (Image/Video Generation)"
    parent = "HuggingFace"
    url = "https://huggingface.co"
    working = True
    needs_auth = True

    tasks = ["text-to-image", "text-to-video"]
    provider_mapping: dict[str, dict] = {}
    task_mapping: dict[str, str] = {}

    @classmethod
    def get_models(cls, **kwargs) -> list[str]:
        if not cls.models:
            url = "https://huggingface.co/api/models?inference=warm&expand[]=inferenceProviderMapping"
            response = requests.get(url)
            if response.ok:
                models = response.json()
                providers = {
                    model["id"]: [
                        provider
                        for provider in model.get("inferenceProviderMapping")
                        if provider.get("status") == "live" and provider.get("task") in cls.tasks
                    ]
                    for model in models
                    if [
                        provider
                        for provider in model.get("inferenceProviderMapping")
                        if provider.get("status") == "live" and provider.get("task") in cls.tasks
                    ]
                }
                new_models = []
                for model, provider_keys in providers.items():
                    new_models.append(model)
                    for provider_data in provider_keys:
                        new_models.append(f"{model}:{provider_data.get('provider')}") 
                cls.task_mapping = {
                    model["id"]: [
                        provider.get("task")
                        for provider in model.get("inferenceProviderMapping")
                    ].pop()
                    for model in models
                }
                prepend_models = []
                for model, provider_keys in providers.items():
                    task = cls.task_mapping.get(model)
                    if task == "text-to-video":
                        prepend_models.append(model)
                        for provider_data in provider_keys:
                            prepend_models.append(f"{model}:{provider_data.get('provider')}") 
                cls.models = prepend_models + [model for model in new_models if model not in prepend_models]
            else:
                cls.models = []
        return cls.models

    @classmethod
    async def get_mapping(cls, model: str, api_key: str = None):
        if model in cls.provider_mapping:
            return cls.provider_mapping[model]
        headers = {
            'Content-Type': 'application/json',
        }
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key}"
        async with StreamSession(
            timeout=30,
            headers=headers,
        ) as session:
            async with session.get(f"https://huggingface.co/api/models/{model}?expand[]=inferenceProviderMapping") as response:
                await raise_for_status(response)
                model_data = await response.json()
                cls.provider_mapping[model] = {key: value for key, value in model_data.get("inferenceProviderMapping").items() if value["status"] == "live"}
        return cls.provider_mapping[model]

    @classmethod
    async def create_async_generator(
        cls,
        model: str,
        messages: Messages,
        api_key: str = None,
        extra_data: dict = {},
        prompt: str = None,
        proxy: str = None,
        timeout: int = 0,
        aspect_ratio: str = "1:1",
        **kwargs
    ):
        selected_provider = None
        if ":" in model:
            model, selected_provider = model.split(":", 1)
        provider_mapping = await cls.get_mapping(model, api_key)
        headers = {
            'Accept-Encoding': 'gzip, deflate',
            'Content-Type': 'application/json',
        }
        new_mapping = {
            "hf-free" if key == "hf-inference" else key: value for key, value in provider_mapping.items()
            if key in ["replicate", "together", "hf-inference"]
        }
        provider_mapping = {**new_mapping, **provider_mapping}
        async def generate(extra_data: dict, prompt: str):
            last_response = None
            for provider_key, provider in provider_mapping.items():
                if selected_provider is not None and selected_provider != provider_key:
                    continue
                provider_info = ProviderInfo(**{**cls.get_dict(), "label": f"HuggingFace ({provider_key})", "url": f"{cls.url}/{model}"})

                api_base = f"https://router.huggingface.co/{provider_key}"
                task = provider["task"]
                provider_id = provider["providerId"]
                if task not in cls.tasks:
                    raise ModelNotSupportedError(f"Model is not supported: {model} in: {cls.__name__} task: {task}")

                prompt = format_image_prompt(messages, prompt)
                if task == "text-to-video":
                    extra_data = {
                        "num_inference_steps": 20,
                        "resolution": "480p",
                        "aspect_ratio": aspect_ratio,
                        **extra_data
                    }
                else:
                    extra_data = use_aspect_ratio(extra_data, aspect_ratio)
                if provider_key == "fal-ai":
                    url = f"{api_base}/{provider_id}"
                    data = {
                        "prompt": prompt,
                        "image_size": "square_hd",
                        **extra_data
                    }
                elif provider_key == "replicate":
                    url = f"{api_base}/v1/models/{provider_id}/predictions"
                    data = {
                        "input": {
                            "prompt": prompt,
                            **extra_data
                        }
                    }
                elif provider_key in ("hf-inference", "hf-free"):
                    api_base = "https://api-inference.huggingface.co"
                    url = f"{api_base}/models/{provider_id}"
                    data = {
                        "inputs": prompt,
                        "parameters": {
                            "seed": random.randint(0, 2**32),
                            **extra_data
                        }
                    }
                elif task == "text-to-image":
                    url = f"{api_base}/v1/images/generations"
                    data = {
                        "response_format": "url",
                        "prompt": prompt,
                        "model": provider_id,
                        **extra_data
                    }

                async with StreamSession(
                    headers=headers if provider_key == "hf-free" or api_key is None else {**headers, "Authorization": f"Bearer {api_key}"},
                    proxy=proxy,
                    timeout=timeout
                ) as session:
                    async with session.post(url, json=data) as response:
                        if response.status in (400, 401, 402):
                            last_response = response
                            debug.error(f"{cls.__name__}: Error {response.status} with {provider_key} and {provider_id}")
                            continue
                        if response.status == 404:
                            raise ModelNotSupportedError(f"Model is not supported: {model}")
                        await raise_for_status(response)
                        async for chunk in save_response_media(response, prompt):
                            return provider_info, chunk
                        result = await response.json()
                        if "video" in result:
                            return provider_info, VideoResponse(result["video"]["url"], prompt)
                        elif task == "text-to-image":
                            return provider_info, ImageResponse([item["url"] for item in result.get("images", result.get("data"))], prompt)
                        elif task == "text-to-video":
                            return provider_info, VideoResponse(result["output"], prompt)
            await raise_for_status(last_response)

        background_tasks = set()
        started = time.time()
        task = asyncio.create_task(generate(extra_data, prompt))
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        while background_tasks:
            yield Reasoning(label="Generating", status=f"{time.time() - started:.2f}s")
            await asyncio.sleep(0.2)
        provider_info, media_response = await task
        yield Reasoning(label="Finished", status=f"{time.time() - started:.2f}s")
        yield provider_info
        yield media_response
