using UnrealBuildTool;

// Headless render service target. Built and run with `-game -RenderOffScreen`
// (not the editor), so this is a Game target. HTTPServer + ProceduralMeshComponent
// are runtime modules and work under a Game target.
public class AutoMeshRenderTarget : TargetRules
{
	public AutoMeshRenderTarget(TargetInfo Target) : base(Target)
	{
		Type = TargetType.Game;
		DefaultBuildSettings = BuildSettingsVersion.V5;
		ExtraModuleNames.Add("AutoMeshRender");
		// We run a -game dev build with no packaged Content/. Without this, the
		// engine calls FShaderCodeLibrary::InitForRuntime and fatal-errors on the
		// missing Global shader pak. Disable the shader code library + pipeline
		// cache + pak file so shaders load from source/disk instead.
		bUseShaderCodeLibrary = false;
		bUseShaderPipelineCache = false;
		bUsePakFile = false;
	}
}
