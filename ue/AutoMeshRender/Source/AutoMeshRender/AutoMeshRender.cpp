#include "AutoMeshRender.h"
#include "Modules/ModuleManager.h"
#include "RenderService.h"

IMPLEMENT_MODULE(FAutoMeshRenderModule, AutoMeshRender)

void FAutoMeshRenderModule::StartupModule()
{
	// Defer until the engine has a valid world / renderer. On a headless -game
	// run the GameViewport is created during engine init; we hook its ready
	// delegate so the HTTP server starts only once rendering is up.
	FCoreDelegates::OnEndFrame.AddStatic(&RenderService::Tick);

	UE_LOG(LogTemp, Log, TEXT("[AutoMeshRender] module started; HTTP service will start on first frame with a viewport."));
}

void FAutoMeshRenderModule::ShutdownModule()
{
	FCoreDelegates::OnEndFrame.RemoveStatic(&RenderService::Tick);
	RenderService::Stop();
	UE_LOG(LogTemp, Log, TEXT("[AutoMeshRender] module stopped."));
}
