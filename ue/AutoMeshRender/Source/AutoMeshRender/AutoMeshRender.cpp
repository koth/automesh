#include "AutoMeshRender.h"
#include "Modules/ModuleManager.h"
#include "RenderService.h"
#include "Misc/CoreDelegates.h"

// Game target primary module — must be IMPLEMENT_PRIMARY_GAME_MODULE (not
// IMPLEMENT_MODULE) so the engine links the startup stack (GForeignEngineDir,
// StdRealloc, etc.) into this executable. Third arg must match the module name
// in AutoMeshRender.Build.cs / .uproject.
IMPLEMENT_PRIMARY_GAME_MODULE(FAutoMeshRenderModule, AutoMeshRender, "AutoMeshRender");

void FAutoMeshRenderModule::StartupModule()
{
	// Defer until the engine has a valid world / renderer. OnEndFrame fires
	// every frame; Tick checks the viewport and starts the HTTP server once.
	// We keep the returned handle because TMulticastDelegate<void()> has no
	// RemoveStatic — only Remove(FDelegateHandle).
	TickHandle = FCoreDelegates::OnEndFrame.AddStatic(&RenderService::Tick);

	UE_LOG(LogTemp, Log, TEXT("[AutoMeshRender] module started; HTTP service will start on first frame with a viewport."));
}

void FAutoMeshRenderModule::ShutdownModule()
{
	if (TickHandle.IsValid())
	{
		FCoreDelegates::OnEndFrame.Remove(TickHandle);
		TickHandle.Reset();
	}
	RenderService::Stop();
	UE_LOG(LogTemp, Log, TEXT("[AutoMeshRender] module stopped."));
}
