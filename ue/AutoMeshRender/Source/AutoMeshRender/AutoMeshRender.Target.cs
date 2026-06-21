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
	}
}
