#pragma once

#include "CoreMinimal.h"

/**
 * Singleton-style persistent render service. Started by the module on first
 * frame; runs an HTTP server on 127.0.0.1 for the lifetime of the process.
 *
 * Endpoints:
 *   POST /reward  {"original_obj": <obj text>, "current_obj": <obj text>,
 *                  "step": int, "faces": int}
 *     -> 200 {"reward": <float>}
 *   POST /render  {"obj": <obj text>, "width": int=1024, "height": int=1024}
 *     -> 200 image/png            (a single 3/4 shaded view of the mesh)
 *      | 200 application/json {"error": ...}   (bad mesh / render failure)
 *
 * Meshes travel as OBJ text in the request body, so the service needs no
 * shared filesystem — it runs from a single HTTP request.
 *
 * The service stays warm: camera setup, render targets and the shader cache
 * are all reused across calls, so each reward is a single render pass rather
 * than a process cold start.
 */
class AUTOMESHRENDER_API RenderService
{
public:
	/** Called every frame from OnEndFrame; starts the server once the viewport is up. */
	static void Tick();
	/** Stops the HTTP server and frees resources. */
	static void Stop();

private:
	static void Start(int32 Port);
	static int32 GetPortFromArgs();
};
