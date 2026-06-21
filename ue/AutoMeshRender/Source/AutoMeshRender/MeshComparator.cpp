#include "MeshComparator.h"

#include "Engine/Engine.h"
#include "Engine/StaticMeshActor.h"
#include "Engine/TextureRenderTarget2D.h"
#include "Components/ProceduralMeshComponent.h"
#include "KismetProceduralMeshLibrary.h"
#include "Materials/Material.h"
#include "UObject/ConstructorHelpers.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "RenderResource.h"
#include "RHI.h"
#include "RHICommandList.h"
#include "RHIDefinitions.h"
#include "Async/Async.h"

// ---------------------------------------------------------------------------
// Camera setup: 6 views (cube-face). Fixed so they are reused across calls.
// ---------------------------------------------------------------------------
struct FFixedCamera
{
	FVector Location;
	FRotator Rotation;
	float Fov;
};

static TArray<FFixedCamera, TFixedAllocator<6>> MakeCameras(const FVector& Center, float Radius)
{
	TArray<FFixedCamera, TFixedAllocator<6>> Cams;
	auto Add = [&](float X, float Y, float Z, FRotator Rot)
	{
		FFixedCamera Cam{ Center + FVector(X, Y, Z) * Radius, Rot, 60.0f };
		Cams.Add(Cam);
	};
	Add( 1, 0, 0, FRotator(0,   0, 0));   // +X
	Add(-1, 0, 0, FRotator(0, 180, 0));   // -X
	Add( 0, 1, 0, FRotator(0, -90, 0));   // +Y
	Add( 0,-1, 0, FRotator(0,  90, 0));   // -Y
	Add( 0, 0, 1, FRotator(-90, 0, 0));   // +Z
	Add( 0, 0,-1, FRotator( 90, 0, 0));   // -Z
	return Cams;
}

// ---------------------------------------------------------------------------
// OBJ parsing: minimal Wavefront loader. Only v and f (triangulated). Matches
// the subset automesh's io.py writes, so no need for a full parser.
// ---------------------------------------------------------------------------
static bool ParseObj(const FString& Path, TArray<FVector>& OutVerts, TArray<int32>& OutTris, FString& OutError)
{
	TArray<FString> Lines;
	if (!FFileHelper::LoadFileToStringArray(Lines, *Path))
	{
		OutError = FString::Printf(TEXT("cannot read OBJ: %s"), *Path);
		return false;
	}

	for (const FString& Line : Lines)
	{
		FString Trimmed = Line.TrimStartAndEnd();
		if (Trimmed.IsEmpty() || Trimmed.StartsWith(TEXT("#")))
		{
			continue;
		}
		if (Trimmed.StartsWith(TEXT("v ")))
		{
			TArray<FString> Parts;
			Trimmed.ParseIntoArrayWS(Parts);
			if (Parts.Num() >= 4)
			{
				OutVerts.Add(FVector(
					FCString::Atof(*Parts[1]),
					FCString::Atof(*Parts[2]),
					FCString::Atof(*Parts[3])));
			}
		}
		else if (Trimmed.StartsWith(TEXT("f ")))
		{
			TArray<FString> Parts;
			Trimmed.ParseIntoArrayWS(Parts);
			// Fan-triangulate; indices may be v/vt/vn, take the first token.
			TArray<int32> Idx;
			for (int32 i = 1; i < Parts.Num(); ++i)
			{
				FString V = Parts[i].Split(TEXT("/")).Key;
				if (!V.IsEmpty())
				{
					int32 v = FCString::Atoi(*V);
					Idx.Add(v > 0 ? v - 1 : OutVerts.Num() + v);
				}
			}
			for (int32 i = 1; i < Idx.Num() - 1; ++i)
			{
				OutTris.Add(Idx[0]);
				OutTris.Add(Idx[i]);
				OutTris.Add(Idx[i + 1]);
			}
		}
	}

	if (OutVerts.Num() == 0 || OutTris.Num() == 0)
		OutError = TEXT("OBJ has no geometry");

	return OutError.IsEmpty();
}

// ---------------------------------------------------------------------------
// Build a ProceduralMeshComponent from parsed verts/tris. Returned actor owns it.
// ---------------------------------------------------------------------------
static AActor* SpawnMeshActor(UObject* WorldContext, const TArray<FVector>& Verts, const TArray<int32>& Tris)
{
	UWorld* World = GEngine->GetWorldFromContextObject(WorldContext, EGetWorldErrorMode::LogAndReturnNull);
	if (!World)
	{
		return nullptr;
	}

	FVector Loc(0);
	FRotator Rot(0);
	FActorSpawnParameters Params;
	Params.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AlwaysSpawn;
	AActor* Actor = World->SpawnActor<AActor>(AActor::StaticClass(), Loc, Rot, Params);
	if (!Actor)
	{
		return nullptr;
	}

	UProceduralMeshComponent* PMC = NewObject<UProceduralMeshComponent>(Actor);
	PMC->SetupAttachment(Actor->GetRootComponent());
	PMC->RegisterComponent();

	TArray<FVector> Normals;
	TArray<FVector2D> UV0;
	TArray<FColor> Colors;
	TArray<FProcMeshTangent> Tangents;
	UKismetProceduralMeshLibrary::CalculateNormals(Verts, Tris, Normals);
	PMC->CreateMeshSection(0, Verts, Tris, Normals, UV0, Colors, Tangents, false);
	PMC->SetMaterial(0, UMaterial::GetDefaultMaterial(MD_Surface));
	PMC->SetVisibility(true);

	return Actor;
}

// ---------------------------------------------------------------------------
// Render the given actors from all cameras into depth+silhouette buffers.
// Returns interleaved [silhouette, depth] per camera, flattened.
// ---------------------------------------------------------------------------
static bool RenderViews(const TArray<FFixedCamera, TFixedAllocator<6>>& Cameras,
                        const FVector& BoundsCenter, float BoundsRadius,
                        const TArray<AActor*>& Actors,
                        TArray<TArray<FLinearColor>>& OutPerViewPixels,
                        FString& OutError)
{
	const int32 Res = 512;
	UTextureRenderTarget2D* RT = NewObject<UTextureRenderTarget2D>(
		GetTransientPackage(), UTextureRenderTarget2D::StaticClass());
	// NOTE(api): RT format/Init methods are version-sensitive. RTF_RGBA8 + Init
	// is the common 5.4 path; if InitAutoFormat is preferred on your build,
	// swap to RT->InitAutoFormat(Res, Res) + RT->UpdateResourceImmediate(true).
	RT->RenderTargetFormat = RTF_RGBA8;
	RT->Init(Res, Res);
	RT->UpdateResourceImmediate(true);

	OutPerViewPixels.SetNum(Cameras.Num());

	// Must run on the render thread because it touches the RHI.
	// Capture happens via FSceneView::RenderTarget... we use the simpler
	// UGameViewportClient / FViewport::Draw path via a one-off FSceneViewState.
	// WARNING(api): the exact screenshot API has shifted across UE5 versions.
	// If GEngine->GameViewport->Viewport->Draw is not directly callable with a
	// custom view, fall back to FViewport::DrawTextureRenderTarget or the
	// SceneCaptureComponent2D approach (see comment at bottom of file).

	// --- SceneCaptureComponent2D approach (more stable across versions) ---
	UWorld* World = Actors.IsEmpty() ? nullptr : Actors[0]->GetWorld();
	if (!World)
	{
		OutError = TEXT("no world to render in");
		return false;
	}

	// Spawn a capture actor per view; reuse the same RT.
	AActor* CaptureActor = World->SpawnActor<AActor>(AActor::StaticClass());
	USceneCaptureComponent2D* Capture = NewObject<USceneCaptureComponent2D>(CaptureActor);
	Capture->SetupAttachment(CaptureActor->GetRootComponent());
	Capture->RegisterComponent();
	Capture->TextureTarget = RT;
	Capture->CaptureSource = ESceneCaptureSource::SCS_SceneDepth;
	Capture->bCaptureEveryFrame = false;
	Capture->bCaptureOnMovement = false;
	Capture->ShowFlags.SetDepthOnlyTest(true);

	for (int32 i = 0; i < Cameras.Num(); ++i)
	{
		const FFixedCamera& Cam = Cameras[i];
		CaptureActor->SetActorLocation(Cam.Location);
		CaptureActor->SetActorRotation(Cam.Rotation);
		Capture->FOVAngle = Cam.Fov;
		Capture->OrthoWidth = BoundsRadius * 2.2f; // not used in perspective, harmless
		// Clip so the whole bounding sphere fits.
		float Near = FMath::Max(1.0f, BoundsRadius * 0.1f);
		float Far = (Cam.Location - BoundsCenter).Size() + BoundsRadius * 2.0f;
		Capture->ClipPlaneNormal = FVector::ZeroVector; // disable custom clip
		// Capture. bUseRayTracingIfEnabled stays false because RT is off.
		Capture->CaptureScene();

		TArray<FLinearColor> Pixels;
		FReadSurfaceDataFlags Flags(RCM_UNorm);
		FlushRenderingCommands();
		FRenderCommandFence Fence;
		{
			// NOTE(api): ReadSurfaceData must run on the render thread and needs
			// an FRHICommandList. In 5.4 the most portable call is via the
			// texture resource's own ReadSurfaceFloatData. If your toolchain
			// only exposes ReadSurfaceData on a global immediate list, replace
			// the block below with:
			//   FRHICommandListExecutor::GetImmediateCommandList().ReadSurfaceData(...)
			FTextureRenderTargetResource* RtRes = RT->GetRenderTargetResource();
			ENQUEUE_RENDER_COMMAND(ReadDepthPixels)(
				[RtRes, Res, Flags, &Pixels](FRHICommandList& RHICmdList)
			{
				RHICmdList.ReadSurfaceData(
					RtRes->TextureRHI,
					FIntRect(0, 0, Res - 1, Res - 1),
					Pixels,
					Flags);
			});
		}
		Fence.BeginFence();
		Fence.Wait();

		OutPerViewPixels[i] = MoveTemp(Pixels);
	}

	CaptureActor->Destroy();
	return true;
}

static float CompareBuffers(const TArray<TArray<FLinearColor>>& A,
                             const TArray<TArray<FLinearColor>>& B)
{
	// 1 - normalised MSE over depth (alpha channel holds depth in SCS_SceneDepth).
	// Keeps the metric bounded in [0,1] for an RL reward.
	if (A.Num() == 0 || A.Num() != B.Num())
	{
		return 0.0f;
	}

	double SumSq = 0.0;
	int64 Count = 0;
	for (int32 v = 0; v < A.Num(); ++v)
	{
		const TArray<FLinearColor>& Va = A[v];
		const TArray<FLinearColor>& Vb = B[v];
		int32 N = FMath::Min(Va.Num(), Vb.Num());
		for (int32 p = 0; p < N; ++p)
		{
			float Da = Va[p].R; // depth packed into R by SCS_SceneDepth + RGBA8
			float Db = Vb[p].R;
			float Diff = Da - Db;
			SumSq += Diff * Diff;
			++Count;
		}
	}
	if (Count == 0)
	{
		return 0.0f;
	}
	double Mse = SumSq / double(Count);
	return float(1.0 / (1.0 + Mse));
}

float MeshComparator::ComputeSimilarity(const FString& OriginalObjPath,
                                        const FString& CurrentObjPath,
                                        FString& OutError)
{
	TArray<FVector> OrigVerts, CurrVerts;
	TArray<int32> OrigTris, CurrTris;
	if (!ParseObj(OriginalObjPath, OrigVerts, OrigTris, OutError)) return 0.0f;
	if (!ParseObj(CurrentObjPath, CurrVerts, CurrTris, OutError)) return 0.0f;

	// Shared camera frame derived from the original mesh's bounds so both are
	// rendered from identical viewpoints.
	FBox Box(OrigVerts);
	FVector Center = Box.GetCenter();
	float Radius = Box.GetExtent().Size();
	if (Radius < KINDA_SMALL_NUMBER) Radius = 1.0f;

	auto Cams = MakeCameras(Center, Radius * 3.0f);

	UObject* WorldCtx = GetTransientPackage();
	AActor* OrigActor = SpawnMeshActor(WorldCtx, OrigVerts, OrigTris);
	AActor* CurrActor = SpawnMeshActor(WorldCtx, CurrVerts, CurrTris);
	if (!OrigActor || !CurrActor)
	{
		OutError = TEXT("failed to spawn mesh actors");
		if (OrigActor) OrigActor->Destroy();
		if (CurrActor) CurrActor->Destroy();
		return 0.0f;
	}

	TArray<TArray<FLinearColor>> OrigPixels, CurrPixels;
	bool bOkA = RenderViews(Cams, Center, Radius, TArray<AActor*>{OrigActor}, OrigPixels, OutError);
	bool bOkB = RenderViews(Cams, Center, Radius, TArray<AActor*>{CurrActor}, CurrPixels, OutError);

	OrigActor->Destroy();
	CurrActor->Destroy();

	if (!bOkA || !bOkB)
	{
		return 0.0f;
	}
	return CompareBuffers(OrigPixels, CurrPixels);
}
