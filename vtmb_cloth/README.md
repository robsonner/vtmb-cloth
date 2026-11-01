# VTMB Cloth — Blender add-on 0.10.0

Create and edit cloth for Vampire: The Masquerade – Bloodlines on the existing
render mesh and real skeleton. The add-on reads and writes the project's
VCLOTH v1 sidecar and exports a matching REF SMD through Blender Source Tools.
It does not create a second cloth mesh, artificial bones or a Blender solver.

This is the current development checkpoint, not a fully validated 1.0 release.
Development is concluded for now; broader regression testing remains deferred.

## Requirements and installation

- Blender: declared minimum 5.0. Development and earlier runtime checks used
  Blender 5.2.1 LTS; this does not establish compatibility with every 5.x build.
- Blender Source Tools, enabled. Version 3.4.3 was used during development;
  the add-on uses public operators rather than private version-specific modules.
- This project's native `studiomdl_vtmb.exe`, with VCLOTH v1 and capsule-helper
  support. Stock Troika/other Source compilers are not interchangeable.
- A complete source project: QC, REF, real skeleton, materials/UVs and associated
  animations/physics/capsules. Existing cloth additionally needs its `.vcloth`.

Install `vtmb_cloth-0.10.0.zip` using Blender **Preferences > Add-ons > Install
from Disk** (in the Add-ons menu), then enable **VTMB Cloth**. Restart Blender
after upgrading. Open the 3D View sidebar with **N**, then select **VTMB Cloth**.
Install the ZIP, not the individual Python files. The package does not include
Source Tools, the compiler or game assets.

## Before starting

1. Work on a copy of the complete source project and save a `.blend` backup.
2. Import its QC with Source Tools, or import the appropriate REF and skeleton.
3. Keep the original paired REF SMD beside the imported `.vcloth`, at the path
   the sidecar records. Keep the original QC available for QC-imported rigs.
   Generation/export verifies this skeleton against a disposable control import.
4. Use one unsplit, triangulated REF with its real Armature modifier. Keep unit
   object scale, original axes/origin, Basis active and neutral shape keys.
5. For capsule placement and export, use the armature's **Rest Position**.
   This means the model's reference pose; it need not form a literal T shape.

The REF is the character's render mesh. `_CAPSULES` and physics meshes are
separate sources: never select them as the REF for cloth attachment.

## Quick start: keep a skirt and add a cape

Use this task when the new cape geometry is already part of the same REF,
weighted to real bones and disconnected from the remaining mesh. It must not
share vertices with the existing body/skirt. Keep the old skirt geometry,
UVs and skinning intact.

1. Choose **Task > Add Cloth Region**.
2. Click **Load Original Cloth** and load the original skirt `.vcloth`.
   If a REF connection already exists, use **Detach REF Mapping** in Object
   Mode. Detaching keeps the loaded cloth data. Do not load cape-only data.
3. Select the REF and press **Tab** for Edit Mode. Press **3** on the main
   keyboard for face selection, deselect with **Alt+A**, then select all cape
   faces and no old skirt/body faces.
4. Click **Capture Surface** directly in the sidebar. Check the triangle count.
5. Switch to vertex selection with **1** on the main keyboard. Deselect and
   select only the cape's attachment points, such as its upper edge at the back.
   Use **Alt+Z** if needed to select through the surface.
6. Click **Set Attachment Points**. These vertices follow the skeleton; the
   remaining captured vertices participate in cloth movement. At least one
   attachment point and one free point are required.
7. Click **Finish Selection**, then **Generate Added Cloth**. Start with the
   existing default parameters. The existing simulation is retained, and the
   new disconnected network shares its time-step scale and instance.
8. After success, click **Export REF + VCLOTH** in Object Mode. Export/compile
   as described below. Capsule export is separate.

**Change Surface** captures the currently selected faces again and resets
attachment points. **Change Attachment Points** replaces only the recorded
anchors. **Cancel Creation** discards the draft, keeping the mesh and loaded
cloth data. Changing geometry/UVs/weights/shading/rig after capture requires
recapture. No nearest-point or face-order guess is used to preserve old cloth.

Unique old-cloth geometry correspondence does not require restoring historical
shading. Add preserves current corner shading and archives the old reference.
Ambiguous protected identities still need exact normal evidence or are rejected.

## Edit existing cloth

Choose **Task > Edit Existing Cloth**, load the matching `.vcloth`, select the
original REF in Object Mode and click **Connect Selected REF**. This connection
validates the original source; an already extended REF should use the additive
workflow above instead.

### Change the shape

1. In Object Mode, choose **Start Shape Edit** before moving any vertices.
2. Enter Edit Mode and move existing vertices, for example to lengthen a skirt.
   Keep topology, UVs, weights, materials and skeleton unchanged.
3. Click **Finish Moving Vertices**, then **Apply Shape Changes**. This updates
   dependent cloth positions/rest lengths and related geometry data.
4. Export the REF/VCLOTH pair. For another shape change, start another session.

**Restore Previous Shape** restores the session's captured positions; it does
not repair deleted faces, changed UVs or weights. Export is blocked until the
shape session is applied or restored. Shape updates may recalculate shading on
affected faces; they do not preserve arbitrary sculpted normals there.

### Change attachment or movement

Select cloth vertices in Edit Mode and choose:

- **Attach to Character**: fixed nodes follow their existing real-bone weights.
  Fixed does not mean stationary in world space.
- **Allow Cloth Movement**: free nodes participate in simulation.

Confirm the dialog. These operations rebuild dependent role data and factor
distribution. They do not change the mesh's bone weights or attach it to a new
bone. Shared render aliases can select/change the same underlying cloth node.

### Edit parameters

Select vertices and use **Edit Selected Cloth Factors**. The dialog chooses
springs with either/both endpoints selected, factors A/B/both and a positive
multiplier. Zeros remain zero and repeated operations multiply current values.
These are raw solver factors, not calibrated stiffness. No generally safe range
is established; a previous manual multiplier 5.0 made a coat disappear.

Role and bulk-factor selection actions support single-object Edit Mode and
restore it after reading the selection. Multi-object Edit Mode is rejected.

## Create cloth from scratch

Choose **Task > Create New Cloth**. If existing data is loaded, unload it
explicitly. This task creates a new graph and does not preserve an old skirt's
simulation; use **Add Cloth Region** when preservation is required.

Capture the complete selected triangular surface, then set its attachment
vertices, click **Finish Selection**, and **Generate Cloth - Choose REF SMD**.
Choose a valid bind-pose REF SMD for the same skeleton. Every connected cloth
region needs an attachment path, and any selected cloth vertex shared with
unselected rigid faces must be attached. An active UV map and one to three
positive real-bone influences per REF point are required.

For a new QC setup, add a source block matching the exported filenames:

```qc
$Cloth "authored_cloth"
{
    model "your_REF.smd"
    source "your_REF.vcloth"
}
```

The label is descriptive; the explicit sidecar identifies render bindings.
The add-on does not create or update this QC block. Generator defaults are
project authoring choices, not recovered Troika production settings.

## Export and compile

1. Finish generation or apply/restore shape edits. Return to Object Mode.
2. Click **Export REF + VCLOTH**, choose the REF filename and an existing
   parent directory. Keep the original REF filename for an existing QC.
3. The add-on creates a new `<REF-stem>_vtmb_sources` folder containing the
   matched `.smd`, `.vcloth` and `export-report.json`. Existing folders are
   rejected rather than overwritten; use another output location for a repeat.
4. Place that pair in a copy of the complete QC project. Keep its animations,
   physics, materials and any procedural-bone files available.
5. Export changed `_CAPSULES.smd` separately through Source Tools and place it
   alongside the QC. See [Capsule guide](CAPSULES.md).
6. Compile with the project's compiler and test movement/collisions in game.

Example, run from the copied QC directory with your actual executable/game paths:

```powershell
& "F:\path\to\studiomdl_vtmb.exe" -vtmb -game "E:\path\to\VtMB\Unofficial_Patch" .\your_model.qc
```

The add-on does not compile, install to the game, export capsules, or rewrite
QC/animations/physics. Its export validates the complete new REF/VCLOTH pair,
including preserved corner identities, source skinning and bind pose.

## Display and diagnostics

- **Viewport Display**: cached static points, springs and render references.
  Blue means attached, orange free, yellow active. This is a bind-pose structure
  display, not a moving simulation preview.
- **Advanced / Diagnostics**: table counts, node/spring IDs, raw single-spring
  editing, explicit REF validation, payload refresh, import, detach and unload.
- Use **Refresh Edited Payload** after manually editing the canonical Text.
  Viewport/panel redraw deliberately does not scan or hash that text each frame.
- Use **Select Cloth Vertices** in Object Mode to select active/fixed/free or
  unbound geometry, then enter Edit Mode to inspect it.

## Limits and common failures

| Limit or message | Meaning / action |
| --- | --- |
| One unsplit REF, one cloth instance, LOD0 | Several disconnected regions can share that instance; separate instances/LODs are unsupported. |
| Separate by Material / later Join | Unsupported preservation workflow. Separating changes decoded corner normals; Join is not an exact repair. |
| Added/removed vertices on existing cloth | No general topology rebind. Use position-only editing, add a disconnected region, or explicitly create a new setup. |
| Extra modifiers, constraints, altered rest rig | Unsupported for the validated REF path. Use the real unchanged Armature modifier. |
| Shape keys / facial animation | Neutral Basis is required; facial VTA authoring and active shape-key export are outside scope. |
| REF changed after capture | Recapture surface and attachment points after mesh/rig changes. |
| Existing cloth faces missing/ambiguous | Preserve its required geometry/UVs/skin/winding; the add-on does not guess identities. |
| Exact normal projection fails on ordinary attachment | Use the unchanged original REF. The additive current-shading policy is not a general rebind waiver. |
| Every point selected as attachment | Leave at least one free point; every connected cloth region needs fixed support. |
| Output directory already exists | Choose a new output location. |
| Capsule pose/weight/shape rejection | Follow [CAPSULES.md](CAPSULES.md); ordinary REF export does not fix capsule sources. |

There is no automatic capsule generator/resizer, solver preview, self-collision
guarantee, animation editor or automatic triangulation. Prepare valid triangles,
UVs and weights before capture. Unknown source fields are retained, but arbitrary
external payload edits are not supported authoring operations.

## Validation status

Manual results include a lengthened skirt working in game, a fixed skirt section,
new Malkavian cape generation and a successful retained skirt + cape workflow.
The user also confirmed the corrected capsule-helper workflow after fixing
blended weights. The 0.10.0 UI received positive review; that is not complete
coverage of every new Edit Mode action, Undo/Redo, saved draft or repeat export.

Earlier automated checks cover original-source attachment, lifecycle and selected
unchanged/spring-edit exports. They do not validate every later authoring feature.
The broader final test series was explicitly deferred. Collision quality, arbitrary
models and general solver-factor stability remain subject to game testing.

## Further documentation

- [Capsule authoring and export](CAPSULES.md)

This guide describes the current UI and supported workflows. Internal design,
development history and agent handover documents are not part of the package.
