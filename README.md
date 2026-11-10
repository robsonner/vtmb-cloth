# VTMB Cloth for Blender — 0.10.3

Tools for inspecting, editing and creating Vampire: The Masquerade – Bloodlines
cloth on the original render mesh and real skeleton. Supports shape edits,
fixed/free points, raw spring factors, new cloth and preserving a skirt while
adding a cape. Exports one matched REF SMD + VCLOTH pair through Source Tools.

Current development checkpoint; a fully validated 1.0 release is not claimed.
Development is concluded for now. Manual workflow results exist, while the
broader final regression series remains deferred.

## Install

Enable Blender Source Tools, then install `vtmb_cloth-0.10.3.zip` using Blender
**Preferences > Add-ons > Install from Disk** and enable **VTMB Cloth**.
Restart Blender after upgrading. Open **3D View > N sidebar > VTMB Cloth**.

The declared minimum is Blender 5.0; development used Blender 5.2.1 LTS and
Source Tools 3.4.3. Compilation requires this project's native VTMB compiler
with VCLOTH v1 and capsule-helper support, not the stock Troika compiler.

## Quick start: a cape alongside an existing skirt

Start with a copy of the complete QC project imported through Source Tools.
Keep the original REF SMD, QC and skirt VCLOTH available. Add the triangulated,
UV-mapped, real-bone-weighted cape to the same REF as a disconnected island.

1. Choose **Task > Add Cloth Region** and **Load Original Cloth** for the skirt.
   If already connected, **Detach REF Mapping** first; keep the data loaded.
2. In Edit Mode, select every new cape face and click **Capture Surface**.
3. Switch to vertex selection, deselect everything and select only the top
   attachment points. Click **Set Attachment Points**.
4. Click **Finish Selection**, then **Generate Added Cloth**.
5. Click **Export REF + VCLOTH** in Object Mode and choose a new output folder.
6. Put the pair into a copy of the complete QC project, export any edited
   CAPSULES SMD separately, compile and test in game.

Choose **Edit Existing Cloth** for shape, attachment and parameter edits.
Choose **Create New Cloth** only when creating a new graph without retaining
the loaded simulation. **Cancel Creation** discards captured selections only.

## Read before editing

- [Full user guide: installation, all workflows, export and troubleshooting](vtmb_cloth/README.md)
- [Capsules: placement, bone weights, materials and separate export](vtmb_cloth/CAPSULES.md)

The supported scope is one unsplit REF, one real rig and one cloth instance
at LOD0; several disconnected cloth regions can share it. General topology
rebinding, Separate by Material, active facial shape keys and additional REF
modifiers/constraints are unsupported. There is no cloth solver preview or
automatic capsule generator. Motion and collision acceptance are in game.

The add-on exports REF + VCLOTH only. QC, animation, physics, capsules and game
installation remain separate. Raw spring factors are not calibrated stiffness;
no generally safe multiplier range is established. Keep project/scene backups.

## Repository and packaging

This is a separate Git repository from the compiler and Crowbar. Source package:
`vtmb_cloth/`. ZIP packages and Python caches are ignored. Shared diagnostic
scripts/reports live in the parent workspace's `tools/` directory.

From that workspace root, build the distributable with:

```powershell
python tools/package_blender_vcloth.py
```

Packaging parses Python syntax and includes the package documentation; it does
not execute Blender, compiler or game tests. The packaged version is 0.10.3.
See the full user guide for supported workflows and validation limits.

### 0.10.3 metadata cleanup

New exports also omit unused `source_model` provenance. Existing values are
discarded on read and export; no original MDL is loaded from this field.

### 0.10.2 file pairing

New exports omit `reference_smd`; existing values are ignored. Blender pairs
REF and VCLOTH by the same filename stem in the same directory. QC remains the
compiler's authority for model/source binding. Real geometry, pose and binding
validation remains enabled. Rename both files together for Blender use.

### 0.10.1 reference-pose correction

Generation/export now proves the live skeleton against the selected REF SMD
with a bounded float32 import tolerance. It no longer searches neighboring QC
files or depends on their ragdoll physics sources. Names, hierarchy and bone
options remain exact checks. The reported QC-imported character passed the
pose proof; translation, rotation, scale, missing-bone, hierarchy, option and
non-finite-transform rejection checks passed. Full cloth export/game testing
for this patch remains pending.
