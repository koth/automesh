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

static bool bStarted = false;
static FHttpRouteHandle RewardRouteHandle = nullptr;
static TSharedPtr<IHttpRouter> Router;

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

			const FString OriginalPath = Body->GetStringField(TEXT("original"));
			const FString CurrentPath = Body->GetStringField(TEXT("current"));
			if (OriginalPath.IsEmpty() || CurrentPath.IsEmpty())
			{
				Respond(OnComplete, EHttpServerResponseCodes::BadRequest, TEXT("{\"error\":\"missing paths\"}"));
				return true;
			}

			// Render + compare synchronously on the request thread. Acceptable at
			// interval=100; if it ever stalls, wrap in AsyncTask to the render
			// thread and return a deferred response.
			// UE disables C++ exceptions by default, so no try/catch — ComputeSimilarity
			// reports failures via OutError (empty = success).
			float Reward = 0.0f;
			FString Error;
			Reward = MeshComparator::ComputeSimilarity(OriginalPath, CurrentPath, Error);
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
			return true;
		}));

	if (!RewardRouteHandle.IsValid())
	{
		UE_LOG(LogTemp, Error, TEXT("[AutoMeshRender] failed to bind /reward route"));
		return;
	}

	Module.StartAllListeners();
	UE_LOG(LogTemp, Log, TEXT("[AutoMeshRender] HTTP service listening on 127.0.0.1:%d/reward"), Port);
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
	if (Router.IsValid() && RewardRouteHandle.IsValid())
	{
		Router->UnbindRoute(RewardRouteHandle);
		RewardRouteHandle = nullptr;
	}
	FHttpServerModule::Get().StopAllListeners();
	Router.Reset();
	UE_LOG(LogTemp, Log, TEXT("[AutoMeshRender] HTTP service stopped."));
}
