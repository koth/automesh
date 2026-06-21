using UnrealBuildTool;

public class AutoMeshRender : ModuleRules
{
	public AutoMeshRender(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = ModuleRules.PCHUsageMode.UseExplicitOrSharedPCHs;

		PublicDependencyModuleNames.AddRange(new string[]
		{
			"Core",
			"CoreUObject",
			"Engine",
			"RenderCore",
			"RHI",
		});

		PrivateDependencyModuleNames.AddRange(new string[]
		{
			"HttpServer",
			"JsonUtilities",
			"Json",
			"ProceduralMeshComponent",
			"Slate",
			"SlateCore",
		});
	}
}
