using UnrealBuildTool;

// Editor target: builds the AutoMeshRender module as libUnrealEditor-AutoMeshRender.so
// so the engine's UnrealEditor can dynamically load it. Use this to run the
// service in uncooked mode (no Content/ pak needed) during development:
//   UnrealEditor AutoMeshRender.uproject -game -RenderOffScreen ...
// The Game target (AutoMeshRender.Target.cs) builds a standalone cooked-mode
// binary and is for packaged runs only.
public class AutoMeshRenderEditorTarget : TargetRules
{
	public AutoMeshRenderEditorTarget(TargetInfo Target) : base(Target)
	{
		Type = TargetType.Editor;
		DefaultBuildSettings = BuildSettingsVersion.V5;
		ExtraModuleNames.Add("AutoMeshRender");
	}
}
