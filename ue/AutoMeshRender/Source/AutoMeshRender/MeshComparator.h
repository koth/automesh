#pragma once

#include "CoreMinimal.h"

/**
 * Core comparison logic: import two OBJs, render each from a fixed set of
 * cameras into render targets, read pixels back, and return a similarity in
 * [0,1] (1 = identical silhouettes/depth, 0 = no overlap). Starting metric is
 * 1 - normalised MSE over multi-view depth+silhouette buffers; upgrade to
 * LPIPS later by swapping CompareBuffers.
 *
 * Error is reported via OutError (empty = success) rather than exceptions so
 * the HTTP layer can return a clean 500 body.
 */
class AUTOMESHRENDER_API MeshComparator
{
public:
	static float ComputeSimilarity(const FString& OriginalObjPath,
	                               const FString& CurrentObjPath,
	                               FString& OutError);
};
