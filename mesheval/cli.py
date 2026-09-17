"""Command-line interface."""
from __future__ import annotations

import argparse
from pathlib import Path

from .metrics import evaluate, load
from .visualize import heatmap


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="evaluate-meshes",
        description="Compare a reconstructed mesh against a reference mesh.")
    p.add_argument("reference", help="reference / more-trusted mesh")
    p.add_argument("test", help="mesh to evaluate")
    p.add_argument("-o", "--out-dir", type=Path, default=Path("results"))
    p.add_argument("-n", "--samples", type=int, default=200_000)
    p.add_argument("-t", "--tolerance", type=float, default=None,
                   help="accuracy threshold in scene units "
                        "(default: 1%% of reference bbox diagonal)")
    p.add_argument("--no-align", action="store_true", help="skip ICP alignment")
    return p


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)

    reference = load(args.reference)
    test = load(args.test)
    result, points, distances = evaluate(
        test, reference, samples=args.samples,
        tolerance=args.tolerance, do_align=not args.no_align)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = heatmap(points, distances, vmax=3.0 * result.tolerance,
                  out_path=args.out_dir / "error_heatmap.ply")

    print(f"\nreference   {args.reference}")
    print(f"test        {args.test}")
    if result.fitness is not None:
        print(f"ICP fitness {result.fitness:.3f}")
    print(f"tolerance   {result.tolerance:.4g} (scene units)\n")
    print(f"  mean        {result.mean:.4g}")
    print(f"  median      {result.median:.4g}")
    print(f"  RMS         {result.rms:.4g}")
    print(f"  Hausdorff   {result.hausdorff:.4g}")
    print(f"  Chamfer     {result.chamfer:.4g}")
    print(f"  within tol  {100.0 * result.within_tol:.1f}%")
    print(f"\nheatmap -> {out}\n")


if __name__ == "__main__":
    main()
