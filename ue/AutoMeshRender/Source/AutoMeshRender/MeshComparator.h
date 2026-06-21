#pragma once

#include "CoreMinimal.h"

/**
 * Core comparison logic: import two OBJs, render each from a fixed set of
 * cameras into render targets, read pixels back, and return a similarity in
 * [0,1] (1 = identical silhouettes/depth, 0 = no overlap). Starting metric is
 * 1 - normalised MSE over multi-view depth+silhouette buffers; upgrade to
 * LPIPS later by swapping CompareBuffers.
 *
 * Meshes arrive as OBJ text (parsed straight from the request body), so the
 * service needs no filesystem access — it runs from a single HTTP request.
 *
 * Error is reported via OutError (empty = success) rather than exceptions so
 * the HTTP layer can return a clean 500 body.
 */
class AUTOMESHRENDER_API MeshComparator
{
public:
	/** Compute similarity from two in-memory OBJ documents. */
	static float ComputeSimilarity(const FString& OriginalObjText,
	                               const FString& CurrentObjText,
	                               FString& OutError);

	/**
	 * Render a single in-memory OBJ to a PNG byte buffer.
	 *
	 * The mesh is auto-framed from a 3/4 perspective camera so it fills the
	 * frame regardless of input bounds. Capture source is the final colour LDR
	 * render (not depth), so the result is a visible shaded image. Width/height
	 * default to 1024.
	 *
	 * @param ObjText       Wavefront OBJ document (v/f lines).
	 * @param Width         Output image width in pixels.
	 * @param Height        Output image height in pixels.
	 * @param OutPngBytes   On success, the encoded PNG bytes.
	 * @param OutError      On failure, a human-readable message (empty = success).
	 * @return              True on success.
	 */
	static bool RenderMeshToPng(const FString& ObjText,
	                            int32 Width, int32 Height,
	                            TArray<uint8>& OutPngBytes,
	                            FString& OutError);
};
