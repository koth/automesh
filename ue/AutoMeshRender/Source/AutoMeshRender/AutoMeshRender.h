#pragma once

#include "CoreMinimal.h"
#include "Delegates/Delegate.h"

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
private:
	/** Handle to the OnEndFrame delegate so we can remove it cleanly on shutdown. */
	FDelegateHandle TickHandle;
};
