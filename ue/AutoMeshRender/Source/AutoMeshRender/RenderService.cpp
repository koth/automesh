#include "RenderService.h"
#include "MeshComparator.h"

#include "Engine/Engine.h"
#include "Engine/GameViewportClient.h"
#include "HttpServerModule.h"
#include "Interfaces/IHttpServer.h"
#include "Dom/JsonObject.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Misc/CommandLine.h"
#include "Misc/Parse.h"

static bool bStarted = false;

int32 RenderService::GetPortFromArgs()
{
	// -RenderServicePort=8765 on the editor command line, per docs/UNREAL_SETUP.md.
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

void RenderService::Start(int32 Port)
{
	IHttpServer& Server = FHttpServerModule::Get().GetServer();

	Server.AddRoute(
		TEXT("/reward"),
		EHttpServerRequestVerbs::VERB_POST,
		FHttpRouteHandler::CreateLambda([](const FHttpServerRequest& Request, const FHttpResultCallback& OnComplete)
		{
			TSharedPtr<FJsonObject> Body;
			{
				TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(
					FString(UTF8_TO_TCHAR(Request.Body.GetData())));
				if (!FJsonSerializer::Deserialize(Reader, Body) || !Body.IsValid())
				{
					auto Resp = MakeUnique<FHttpServerResponse>();
					Resp->Code = EHttpServerResponseCodes::BadRequest;
					OnComplete(MoveTemp(Resp));
					return;
				}
			}

			const FString OriginalPath = Body->GetStringField(TEXT("original"));
			const FString CurrentPath = Body->GetStringField(TEXT("current"));
			if (OriginalPath.IsEmpty() || CurrentPath.IsEmpty())
			{
				auto Resp = MakeUnique<FHttpServerResponse>();
				Resp->Code = EHttpServerResponseCodes::BadRequest;
				OnComplete(MoveTemp(Resp));
				return;
			}

			// Render + compare on the game thread. MeshComparator is synchronous
			// here; for the first cut this blocks the request thread, which is
			// acceptable at interval=100. If it ever stalls the game thread,
			// wrap in AsyncTask to the render thread and return a deferred
			// response.
			float Reward = 0.0f;
			bool bOk = false;
			FString Error;
			try
			{
				Reward = MeshComparator::ComputeSimilarity(OriginalPath, CurrentPath, Error);
				bOk = Error.IsEmpty();
			}
			catch (const std::exception& Exc)
			{
				Error = FString(ANSI_TO_TCHAR(Exc.what()));
			}

			TSharedPtr<FJsonObject> RespJson = MakeShareable(new FJsonObject);
			RespJson->SetNumberField(TEXT("reward"), Reward);

			auto Resp = MakeUnique<FHttpServerResponse>();
			Resp->Code = bOk ? EHttpServerResponseCodes::Ok : EHttpServerResponseCodes::InternalServerError;
			Resp->Body = TCHAR_TO_UTF8(*BuildJsonResponse(RespJson));
			TArray<FString> ContentType;
			ContentType.Add(TEXT("application/json"));
			Resp->Headers.Add(TEXT("Content-Type"), ContentType);
			OnComplete(MoveTemp(Resp));
		}));

	Server.Start(Port);
	UE_LOG(LogTemp, Log, TEXT("[AutoMeshRender] HTTP service listening on 127.0.0.1:%d/reward"), Port);
}

void RenderService::Tick()
{
	if (bStarted)
	{
		return;
	}
	if (!GEngine || !GEngine->GameViewport.IsValid())
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
	FHttpServerModule::Get().GetServer().Stop();
	UE_LOG(LogTemp, Log, TEXT("[AutoMeshRender] HTTP service stopped."));
}
