#pragma once

#include "CoreMinimal.h"

/**
 * AutoMeshRender module: a persistent headless render service.
 *
 * On startup it forces a transient empty world (no map asset needed), spins up
 * an HTTP listener on 127.0.0.1:<port>, and serves POST /reward by importing
 * two OBJ meshes, rendering each from fixed cameras, and returning a similarity
 * float. Designed to be called by automesh's HttpRenderReward.
 */
class FAutoMeshRenderModule : public IModuleInterface
{
public:
	virtual void StartupModule() override;
	virtual void ShutdownModule() override;
};
