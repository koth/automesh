#include "RenderService.h"
#include "MeshComparator.h"

#include "Engine/Engine.h"
#include "Engine/GameViewportClient.h"
#include "HttpServerModule.h"
#include "IHttpRouter.h"
#include "HttpRequestHandler.h"
#include "HttpResultCallback.h"
#include "HttpServerRequest.h"
#include "HttpServerResponse.h"
#include "HttpPath.h"
#include "Dom/JsonObject.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Misc/CommandLine.h"
#include "Misc/Parse.h"
#include "Async/Async.h"
#include "HAL/Event.h"
#include "Misc/ScopeExit.h"

static bool bStarted = false;
static FHttpRouteHandle RewardRouteHandle = nullptr;
static FHttpRouteHandle RenderRouteHandle = nullptr;
static TSharedPtr<IHttpRouter> Router;

// Run `Work` on the game thread synchronously. The HTTPServer module invokes
// route handlers on its own listener thread; SpawnActor / CaptureScene /
// GetRenderTargetResource all require the game thread, and calling them from
// the listener thread crashes with `check(RenderTargetResource)` (or similar
// engine asserts) because the RT was never given a chance to initialize.
static void RunOnGameThread(TFunction<void()> Work)
{
	if (IsInGameThread())
	{
		Work();
		return;
	}
	FEvent* Done = FPlatformProcess::GetSynchEventFromPool(false);
	ON_SCOPE_EXIT { FPlatformProcess::ReturnSynchEventToPool(Done); };
	AsyncTask(ENamedThreads::GameThread, [Work = MoveTemp(Work), Done]()
	{
		Work();
		Done->Trigger();
	});
	Done->Wait();
}

int32 RenderService::GetPortFromArgs()
{
	// -RenderServicePort=8765 on the command line, per docs/UNREAL_SETUP.md.
	int32 Port = 8765;
	FParse::Value(FCommandLine::Get(), TEXT("RenderServicePort="), Port);
	return Port;
}

static FString BuildJsonResponse(TSharedRef<FJsonObject> Object)
{
	FString Out;
	TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Out);
	FJsonSerializer::Serialize(Object, Writer);
	return Out;
}

static void Respond(const FHttpResultCallback& OnComplete, EHttpServerResponseCodes Code, const FString& JsonBody)
{
	// Encode the JSON string as UTF-8 bytes (FHttpServerResponse::Body is TArray<uint8>).
	const FTCHARToUTF8 Utf8(*JsonBody);
	TArray<uint8> BodyBytes;
	BodyBytes.Append(reinterpret_cast<const uint8*>(Utf8.Get()), Utf8.Length());

	auto Resp = MakeUnique<FHttpServerResponse>(MoveTemp(BodyBytes));
	Resp->Code = Code;
	TArray<FString> ContentType;
	ContentType.Add(TEXT("application/json"));
	Resp->Headers.Add(TEXT("Content-Type"), ContentType);
	OnComplete(MoveTemp(Resp));
}

// Respond with raw binary bytes and a caller-supplied Content-Type (e.g. PNG).
static void RespondBytes(const FHttpResultCallback& OnComplete, EHttpServerResponseCodes Code,
                         TArray<uint8> BodyBytes, const FString& ContentType)
{
	auto Resp = MakeUnique<FHttpServerResponse>(MoveTemp(BodyBytes));
	Resp->Code = Code;
	TArray<FString> Type;
	Type.Add(ContentType);
	Resp->Headers.Add(TEXT("Content-Type"), Type);
	OnComplete(MoveTemp(Resp));
}

void RenderService::Start(int32 Port)
{
	FHttpServerModule& Module = FHttpServerModule::Get();

	// GetHttpRouter binds the port and creates the listener in one call — there
	// is no separate Start(port). bFailOnBindFailure=true so a port clash fails
	// loudly instead of silently returning a dead router.
	Router = Module.GetHttpRouter(static_cast<uint32>(Port), /*bFailOnBindFailure=*/true);
	if (!Router.IsValid())
	{
		UE_LOG(LogTemp, Error, TEXT("[AutoMeshRender] failed to bind HTTP router on port %d"), Port);
		return;
	}

	RewardRouteHandle = Router->BindRoute(
		FHttpPath(TEXT("/reward")),
		EHttpServerRequestVerbs::VERB_POST,
		FHttpRequestHandler::CreateLambda([](const FHttpServerRequest& Request, const FHttpResultCallback& OnComplete) -> bool
		{
			// Body is TArray<uint8>; parse as UTF-8 JSON.
			FString BodyStr(UTF8_TO_TCHAR(Request.Body.GetData()));
			TSharedPtr<FJsonObject> Body;
			TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(BodyStr);
			if (!FJsonSerializer::Deserialize(Reader, Body) || !Body.IsValid())
			{
				Respond(OnComplete, EHttpServerResponseCodes::BadRequest, TEXT("{\"error\":\"bad json\"}"));
				return true;
			}

			const FString OriginalObjText = Body->GetStringField(TEXT("original_obj"));
			const FString CurrentObjText = Body->GetStringField(TEXT("current_obj"));
			if (OriginalObjText.IsEmpty() || CurrentObjText.IsEmpty())
			{
				Respond(OnComplete, EHttpServerResponseCodes::BadRequest, TEXT("{\"error\":\"missing obj content\"}"));
				return true;
			}

			// Render + compare on the game thread — SpawnActor / CaptureScene
			// require it; calling them from the HTTPServer listener thread crashes.
			// UE disables C++ exceptions by default, so no try/catch —
			// ComputeSimilarity reports failures via OutError (empty = success).
			RunOnGameThread([OriginalObjText, CurrentObjText, OnComplete]()
			{
				float Reward = 0.0f;
				FString Error;
				Reward = MeshComparator::ComputeSimilarity(OriginalObjText, CurrentObjText, Error);
				bool bOk = Error.IsEmpty();

				TSharedPtr<FJsonObject> RespJson = MakeShareable(new FJsonObject);
				RespJson->SetNumberField(TEXT("reward"), Reward);
				if (!bOk)
				{
					RespJson->SetStringField(TEXT("error"), Error);
				}
				// Always return HTTP 200 with the error in the JSON body — the exact
				// EHttpServerResponseCodes error enum value names vary across UE5
				// builds, and HttpRenderReward only reads the JSON "reward" field
				// anyway (it raises on non-2xx, so a 200-with-error is safer for
				// debugging a misbehaving comparator than killing the training step).
				EHttpServerResponseCodes Code = EHttpServerResponseCodes::Ok;
				Respond(OnComplete, Code, BuildJsonResponse(RespJson.ToSharedRef()));
			});
			return true;
		}));

	if (!RewardRouteHandle.IsValid())
	{
		UE_LOG(LogTemp, Error, TEXT("[AutoMeshRender] failed to bind /reward route"));
		return;
	}

	// POST /render  {"obj": <obj text>, "width": int=1024, "height": int=1024}
	//   -> 200 image/png (a single shaded 3/4 view of the mesh)
	//   -> 200 application/json {"error": ...} on a bad mesh / render failure
	RenderRouteHandle = Router->BindRoute(
		FHttpPath(TEXT("/render")),
		EHttpServerRequestVerbs::VERB_POST,
		FHttpRequestHandler::CreateLambda([](const FHttpServerRequest& Request, const FHttpResultCallback& OnComplete) -> bool
		{
			FString BodyStr(UTF8_TO_TCHAR(Request.Body.GetData()));
			TSharedPtr<FJsonObject> Body;
			TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(BodyStr);
			if (!FJsonSerializer::Deserialize(Reader, Body) || !Body.IsValid())
			{
				Respond(OnComplete, EHttpServerResponseCodes::BadRequest, TEXT("{\"error\":\"bad json\"}"));
				return true;
			}

			const FString ObjText = Body->GetStringField(TEXT("obj"));
			if (ObjText.IsEmpty())
			{
				Respond(OnComplete, EHttpServerResponseCodes::BadRequest, TEXT("{\"error\":\"missing obj content\"}"));
				return true;
			}
			int32 Width = 1024;
			int32 Height = 1024;
			Body->TryGetNumberField(TEXT("width"), Width);
			Body->TryGetNumberField(TEXT("height"), Height);

			// Render + PNG encode on the game thread — same reason as /reward.
			RunOnGameThread([ObjText, Width, Height, OnComplete]()
			{
				TArray<uint8> PngBytes;
				FString Error;
				bool bOk = MeshComparator::RenderMeshToPng(ObjText, Width, Height, PngBytes, Error);
				if (!bOk)
				{
					TSharedPtr<FJsonObject> ErrJson = MakeShareable(new FJsonObject);
					ErrJson->SetStringField(TEXT("error"), Error);
					Respond(OnComplete, EHttpServerResponseCodes::Ok, BuildJsonResponse(ErrJson.ToSharedRef()));
					return;
				}

				RespondBytes(OnComplete, EHttpServerResponseCodes::Ok, MoveTemp(PngBytes), TEXT("image/png"));
			});
			return true;
		}));

	if (!RenderRouteHandle.IsValid())
	{
		UE_LOG(LogTemp, Error, TEXT("[AutoMeshRender] failed to bind /render route"));
		return;
	}

	Module.StartAllListeners();
	UE_LOG(LogTemp, Log, TEXT("[AutoMeshRender] HTTP service listening on 127.0.0.1:%d/reward and /render"), Port);
}

void RenderService::Tick()
{
	if (bStarted)
	{
		return;
	}
	if (!GEngine || !GEngine->GameViewport)
	{
		return; // wait for the headless viewport to exist
	}
	bStarted = true;
	Start(GetPortFromArgs());
}

void RenderService::Stop()
{
	if (!bStarted)
	{
		return;
	}
	bStarted = false;
	if (Router.IsValid())
	{
		if (RewardRouteHandle.IsValid())
		{
			Router->UnbindRoute(RewardRouteHandle);
			RewardRouteHandle = nullptr;
		}
		if (RenderRouteHandle.IsValid())
		{
			Router->UnbindRoute(RenderRouteHandle);
			RenderRouteHandle = nullptr;
		}
	}
	FHttpServerModule::Get().StopAllListeners();
	Router.Reset();
	UE_LOG(LogTemp, Log, TEXT("[AutoMeshRender] HTTP service stopped."));
}
