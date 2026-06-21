#include "MeshComparator.h"

#include "Engine/Engine.h"
#include "Engine/StaticMeshActor.h"
#include "Engine/TextureRenderTarget2D.h"
#include "ProceduralMeshComponent.h"
#include "KismetProceduralMeshLibrary.h"
#include "Materials/Material.h"
#include "MaterialDomain.h"
#include "Components/SceneCaptureComponent2D.h"
#include "UObject/ConstructorHelpers.h"
#include "RenderResource.h"
#include "RHI.h"
#include "RHICommandList.h"
#include "RHIDefinitions.h"
#include "Async/Async.h"

#include "IImageWrapper.h"
#include "IImageWrapperModule.h"
#include "Modules/ModuleManager.h"

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
// the subset automesh's io.py writes (mesh_to_obj_string), so no need for a
// full parser. Operates on an in-memory OBJ document; no filesystem access.
// ---------------------------------------------------------------------------
static bool ParseObjFromString(const FString& ObjText, TArray<FVector>& OutVerts, TArray<int32>& OutTris, FString& OutError)
{
	TArray<FString> Lines;
	ObjText.ParseIntoArrayLines(Lines);

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
				// Index tokens may be v, v/vt, v//vn, or v/vt/vn. Take the part
				// before the first '/'. FString::Split writes via out-params.
				FString V = Parts[i];
				FString Left, Right;
				if (Parts[i].Split(TEXT("/"), &Left, &Right))
				{
					V = Left;
				}
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
	// CalculateTangentsForMesh fills both Normals and Tangents; needs UVs (can be empty array).
	UKismetProceduralMeshLibrary::CalculateTangentsForMesh(Verts, Tris, UV0, Normals, Tangents);
	PMC->CreateMeshSection(0, Verts, Tris, Normals, UV0, Colors, Tangents, false);
		PMC->SetMaterial(0, UMaterial::GetDefaultMaterial(EMaterialDomain::MD_Surface));
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
                        TArray<TArray<FColor>>& OutPerViewPixels,
                        FString& OutError)
{
	const int32 Res = 512;
	UTextureRenderTarget2D* RT = NewObject<UTextureRenderTarget2D>(
		GetTransientPackage(), UTextureRenderTarget2D::StaticClass());
	// NOTE(api): RT format/Init methods are version-sensitive. RTF_RGBA8 + Init
	// is the common 5.4 path; if InitAutoFormat is preferred on your build,
	// swap to RT->InitAutoFormat(Res, Res) + RT->UpdateResourceImmediate(true).
	RT->RenderTargetFormat = RTF_RGBA8;
	RT->InitAutoFormat(Res, Res);
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

		TArray<FColor> Pixels;
		FReadSurfaceDataFlags Flags(RCM_UNorm);
		FlushRenderingCommands();
		FRenderCommandFence Fence;
		{
			// ReadSurfaceData is on FRHICommandList (RHICommandList.h), must run
			// on the render thread. Pixels are FColor (the FColor overload).
			FTextureRenderTargetResource* RtRes = RT->GetRenderTargetResource();
			ENQUEUE_RENDER_COMMAND(ReadDepthPixels)(
				[RtRes, Res, Flags, &Pixels](FRHICommandListImmediate& RHICmdList)
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

static float CompareBuffers(const TArray<TArray<FColor>>& A,
                             const TArray<TArray<FColor>>& B)
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
	const TArray<FColor>& Va = A[v];
	const TArray<FColor>& Vb = B[v];
		int32 N = FMath::Min(Va.Num(), Vb.Num());
		for (int32 p = 0; p < N; ++p)
		{
			float Da = Va[p].R / 255.0f; // FColor.R is uint8; normalise to [0,1]
			float Db = Vb[p].R / 255.0f;
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

float MeshComparator::ComputeSimilarity(const FString& OriginalObjText,
                                        const FString& CurrentObjText,
                                        FString& OutError)
{
	TArray<FVector> OrigVerts, CurrVerts;
	TArray<int32> OrigTris, CurrTris;
	if (!ParseObjFromString(OriginalObjText, OrigVerts, OrigTris, OutError)) return 0.0f;
	if (!ParseObjFromString(CurrentObjText, CurrVerts, CurrTris, OutError)) return 0.0f;

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

	TArray<TArray<FColor>> OrigPixels, CurrPixels;
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

// ---------------------------------------------------------------------------
// PNG encoding via the ImageWrapper module (stable across UE4/5). FColor is
// BGRA in memory; the wrapper's ERGBFormat::BGRA matches it byte-for-byte.
// ---------------------------------------------------------------------------
static bool EncodePng(const TArray<FColor>& Pixels, int32 Width, int32 Height,
                      TArray<uint8>& OutBytes, FString& OutError)
{
	if (Pixels.Num() != Width * Height)
	{
		OutError = TEXT("pixel count does not match width*height");
		return false;
	}

	IImageWrapperModule& ImageWrapperModule = FModuleManager::LoadModuleChecked<IImageWrapperModule>(FName("ImageWrapper"));
	TSharedPtr<IImageWrapper> PngWrapper = ImageWrapperModule.CreateImageWrapper(EImageFormat::PNG);
	if (!PngWrapper.IsValid())
	{
		OutError = TEXT("PNG image wrapper unavailable");
		return false;
	}

	// FColor layout in memory is B,G,R,A -> ERGBFormat::BGRA, 8 bits/channel.
	if (!PngWrapper->SetRaw(Pixels.GetData(), Pixels.Num() * sizeof(FColor),
	                        Width, Height, ERGBFormat::BGRA, 8))
	{
		OutError = TEXT("failed to set raw image data for PNG");
		return false;
	}

	// GetCompressed returns TArray64<uint8> (64-bit count) and takes an int32
	// quality (0 = default). Copy into OutBytes (TArray<uint8>) by pointer+count;
	// a direct assign would slice the 64-bit size type.
	TArray64<uint8> Compressed = PngWrapper->GetCompressed(0);
	if (Compressed.Num() <= 0)
	{
		OutError = TEXT("PNG compression produced no bytes");
		return false;
	}
	OutBytes.Reset(static_cast<int32>(Compressed.Num()));
	OutBytes.Append(reinterpret_cast<const uint8*>(Compressed.GetData()), static_cast<int32>(Compressed.Num()));
	return true;
}

bool MeshComparator::RenderMeshToPng(const FString& ObjText,
                                     int32 Width, int32 Height,
                                     TArray<uint8>& OutPngBytes,
                                     FString& OutError)
{
	Width = FMath::Clamp(Width, 1, 4096);
	Height = FMath::Clamp(Height, 1, 4096);

	TArray<FVector> Verts;
	TArray<int32> Tris;
	if (!ParseObjFromString(ObjText, Verts, Tris, OutError))
	{
		return false;
	}

	FBox Box(Verts);
	FVector Center = Box.GetCenter();
	float Radius = Box.GetExtent().Size();
	if (Radius < KINDA_SMALL_NUMBER) Radius = 1.0f;

	UObject* WorldCtx = GetTransientPackage();
	AActor* MeshActor = SpawnMeshActor(WorldCtx, Verts, Tris);
	if (!MeshActor)
	{
		OutError = TEXT("failed to spawn mesh actor");
		return false;
	}

	// 3/4 perspective view: iso direction, auto-framed so the bounding sphere
	// fits inside the FOV. Distance = Radius / sin(FOV/2) keeps the whole sphere
	// on screen; we add 10% slack so edges aren't clipped by the near/far planes.
	const float FovDeg = 45.0f;
	const float FovRad = FMath::DegreesToRadians(FovDeg);
	const float HalfSin = FMath::Max(KINDA_SMALL_NUMBER, FMath::Sin(FovRad * 0.5f));
	const float Dist = (Radius / HalfSin) * 1.1f;

	FVector Dir(1.0, 1.0, 1.0);
	Dir.Normalize();
	FVector CamPos = Center + Dir * Dist;
	FRotator CamRot = FRotationMatrix::MakeFromX(-Dir).Rotator();

	UWorld* World = MeshActor->GetWorld();
	if (!World)
	{
		OutError = TEXT("no world to render in");
		MeshActor->Destroy();
		return false;
	}

	UTextureRenderTarget2D* RT = NewObject<UTextureRenderTarget2D>(
		GetTransientPackage(), UTextureRenderTarget2D::StaticClass());
	RT->RenderTargetFormat = RTF_RGBA8;
	RT->InitAutoFormat(Width, Height);
	RT->UpdateResourceImmediate(true);

	AActor* CaptureActor = World->SpawnActor<AActor>(AActor::StaticClass());
	USceneCaptureComponent2D* Capture = NewObject<USceneCaptureComponent2D>(CaptureActor);
	Capture->SetupAttachment(CaptureActor->GetRootComponent());
	Capture->RegisterComponent();
	Capture->TextureTarget = RT;
	// Final colour LDR (not depth): a visible shaded image of the mesh.
	Capture->CaptureSource = ESceneCaptureSource::SCS_FinalColorLDR;
	Capture->bCaptureEveryFrame = false;
	Capture->bCaptureOnMovement = false;
	Capture->FOVAngle = FovDeg;

	CaptureActor->SetActorLocation(CamPos);
	CaptureActor->SetActorRotation(CamRot);
	Capture->ClipPlaneNormal = FVector::ZeroVector; // disable custom clip
	Capture->CaptureScene();

	TArray<FColor> Pixels;
	FReadSurfaceDataFlags Flags(RCM_UNorm);
	FlushRenderingCommands();
	FRenderCommandFence Fence;
	{
		FTextureRenderTargetResource* RtRes = RT->GetRenderTargetResource();
		ENQUEUE_RENDER_COMMAND(ReadRenderPixels)(
			[RtRes, Width, Height, Flags, &Pixels](FRHICommandListImmediate& RHICmdList)
		{
			RHICmdList.ReadSurfaceData(
				RtRes->TextureRHI,
				FIntRect(0, 0, Width - 1, Height - 1),
				Pixels,
				Flags);
		});
	}
	Fence.BeginFence();
	Fence.Wait();

	CaptureActor->Destroy();
	MeshActor->Destroy();

	return EncodePng(Pixels, Width, Height, OutPngBytes, OutError);
}
