# Cloth collision capsules

Capsules are separate collision sources for the compiler. They are not REF
geometry and are not exported by **Export REF + VCLOTH**. Use the original
Blender Source Tools exporter for the complete `_CAPSULES` object.

Use this project's compiler with the composed capsule bind-pose check
(`c77da50` or later). This permits equivalent Euler representations while
checking actual bound-bone rest transforms. Source Tools is not patched.

## What a capsule represents

A capsule is a straight circular tube with hemispherical ends, encoded as two
axis endpoints, one radius and a real bone binding. The visible helper mesh lets
the compiler recover those values. It is not an arbitrary collision mesh.

- Each capsule must be a closed, connected, round surface with valid poles and
  equators. A sphere-like shape with no axis length is rejected.
- Each capsule needs its own numbered material, such as `__vtmb_capsule_0002`.
  Do not assign one material to two disconnected capsules.
- Every vertex of one capsule must have one full-weight real-bone assignment;
  all its vertices must use the same bone. No blended/two-bone helper capsules.
- Keep the original armature, reference frame, object transforms and alignment.

## Add a capsule on the back

The following example assumes the QC imported an existing `_CAPSULES` mesh.
Make a scene/source backup first.

1. Select the armature. In its data properties, **Skeleton > Rest Position**
   shows the model's bind pose without deleting animation. Place/export capsule
   geometry in this reference pose, not in an animated frame's deformation.
2. Select the object ending in `_CAPSULES`, then **Tab** into Edit Mode.
3. Choose face selection (**3**, main keyboard), deselect with **Alt+A** and
   open Material Properties. Choose an existing capsule material and click
   **Select** to select the complete capsule, including any vertex seams.
4. Duplicate with **Shift+D**, then right-click to cancel the initial movement.
   The copy remains selected in the same object.
5. Use **G**, **R** and uniform **S** to place, rotate and size it along the back.
   Keep it mostly inside the torso, with its back surface near the character's
   back and the cloth starting outside it. Excessive protrusion pushes cloth
   away; game testing is needed to assess the actual placement.
6. With only the copied capsule selected, add an empty material slot using
   **+**, choose it and click **New**. Give it an unused numbered name such as
   `__vtmb_capsule_0002`, then click **Assign**. Do not rename a material still
   shared with an original capsule.
7. Open mesh data properties (green triangle), then **Vertex Groups**. The copy
   inherited its original bone weights. Select each former bone group and click
   **Remove** below the list, removing membership of the selected vertices.
   For a copied Malkavian thigh capsule, check `Bip01 L Thigh` / `Bip01 R Thigh`.
   Remove any other inherited/previous assignments as well. Do not delete entire
   groups using minus: the other capsules may still need them.
8. Choose the real upper-torso group, for example `Bip01 Spine2` on Malkavian.
   If absent, add a group with that exact real-bone name. Set **Weight: 1.000**
   and click **Assign**. All new-capsule vertices must have this one assignment.
9. Return to Object Mode, keep the capsule object visible/selected, and use
   Source Tools' SMD export for that object/export entry. Export the entire
   capsule object, keeping both original and added capsules.
10. Put the exported `_CAPSULES.smd` beside the QC in the copied compilation
    project. Compile again with the updated native compiler, then test in game.

Once exported, **Pose Position** can be restored to preview animation.
Membership can be checked without a Vertex Weights panel: deselect everything,
choose a group in Vertex Groups and click **Select**. This selects that group's
member vertices. Deselect before checking another group. Membership selection
alone does not prove numeric weight values; explicitly assign weight 1 to the
complete capsule after removing other memberships.

## QC connection

Existing imported QC projects may already have the helper body declaration.
Keep it and its filename rather than adding a duplicate. For a new setup:

```qc
$Body "__vtmb_cloth_capsules" "your_REF_CAPSULES.smd" model "your_REF.smd"
```

The compiler treats this reserved body as authoring input, not render geometry.
It recovers the helper capsules and replaces the target's legacy same-bone QC
capsule records. Split-bone QC records and constraint capsules remain separate;
they are not authored by these helper meshes. The explicit model binding avoids
ambiguous targets in QC projects with multiple render models.

## Resizing and failures

Uniform **S** preserves the shape. **S X**, **S Y** or **S Z** on the whole
capsule stretches the round ends or makes an oval cross-section and can cause
rejection. To change length independently, translate an entire end-cap half
along the capsule axis without deforming its hemisphere; the middle tube spans
the new gap. Independently changing radius requires rebuilding a geometrically
valid tube and hemispheres. There is currently no radius/length editing tool.
Moving only a few end vertices is not a valid capsule resize.

| Compiler message | Check |
| --- | --- |
| One full-weight real bone required | Remove inherited memberships on all points of the offending capsule, then assign one real group at weight 1. |
| Different bone bindings within one capsule | All its points must follow the same bone. |
| Disconnected capsules share one material | Assign a different numbered material to each capsule. |
| Not a round capsule / degenerate | Undo nonuniform scaling, deformation or missing surface faces. |
| Reference pose mismatch on bound bone | Export against the same real rig in Rest Position; use the updated compiler. Actual pose changes are still rejected. |
| Helper target missing/ambiguous | Check the helper path and explicit QC model binding. |

Capsule presence does not guarantee complete collision coverage. This add-on
does not simulate collision in Blender; test the moving outfit in game.
