"""CLI do ViceClips.

  viceclips web                      # interface no navegador (http://127.0.0.1:8000)
  viceclips render trecho.mp4 --title "GTA 6 Alien Abduction"
  viceclips vod URL --start 1:23:45 --end 1:24:30
  viceclips vod URL --auto 5
  viceclips live URL                 # mantém o buffer da live rodando
  viceclips live-clip --last 45      # corta os últimos 45s do buffer e renderiza
"""
import argparse
import time

from . import config
from .config import Rect
from .media import parse_time
from .pipeline import make_clip


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--title", help="título do clip (omitido: gerado por IA, se configurada)")
    p.add_argument("--cam", type=Rect.parse, metavar="X,Y,W,H",
                   help="posição da câmera no vídeo de origem (omitido: detecção automática)")
    p.add_argument("--credit", help="crédito do streamer, ex.: @canal")
    p.add_argument("--out", help="arquivo de saída")


def _render(args, src: str, default_name: str) -> None:
    out = args.out or config.OUT_DIR / f"{default_name}.mp4"
    print(make_clip(src, out, title=args.title, cam=args.cam, credit=args.credit))


def cmd_render(args) -> None:
    _render(args, args.file, f"clip_{int(time.time())}")


def cmd_vod(args) -> None:
    from .ingest import vod

    stamp = int(time.time())
    if args.auto:
        from .highlights.audio import find_highlights
        audio = vod.download_audio(args.url, config.WORK_DIR / f"vod_{stamp}.m4a")
        windows = [(s, e) for s, e, _ in find_highlights(str(audio), count=args.auto)]
        if not windows:
            raise SystemExit("Nenhum pico encontrado; informe --start/--end.")
    elif args.start and args.end:
        windows = [(parse_time(args.start), parse_time(args.end))]
    else:
        raise SystemExit("Informe --start e --end, ou --auto N.")

    for i, (start, end) in enumerate(windows, 1):
        raw = vod.download_section(args.url, start, end, config.WORK_DIR / f"vod_{stamp}_{i}.mp4")
        args_out = args.out if len(windows) == 1 else None
        out = args_out or config.OUT_DIR / f"clip_{stamp}_{i}.mp4"
        print(make_clip(str(raw), out, title=args.title, cam=args.cam, credit=args.credit))


def cmd_live(args) -> None:
    from .ingest import live

    proc = live.record(args.url, config.WORK_DIR / "live")
    print(f"Gravando buffer em {config.WORK_DIR / 'live'} (Ctrl+C para parar). "
          f"Em outro terminal: viceclips live-clip --last 45")
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()


def cmd_live_clip(args) -> None:
    from .ingest import live

    stamp = int(time.time())
    raw = live.cut_last(config.WORK_DIR / "live", args.last, config.WORK_DIR / f"live_{stamp}.mp4")
    _render(args, str(raw), f"clip_{stamp}")


def cmd_web(args) -> None:
    import os

    import uvicorn

    if args.host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get("VICECLIPS_PASSWORD"):
        raise SystemExit("Para abrir o site fora desta máquina, defina a senha: "
                         "VICECLIPS_PASSWORD=... viceclips web --host 0.0.0.0")
    print(f"ViceClips em http://{args.host}:{args.port}")
    uvicorn.run("viceclips.web.app:app", host=args.host, port=args.port, log_level="warning")


def main() -> None:
    parser = argparse.ArgumentParser(prog="viceclips", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("web", help="interface web")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("render", help="arquivo local já recortado -> clip vertical")
    p.add_argument("file")
    _common(p)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("vod", help="live gravada (link) -> clip(s)")
    p.add_argument("url")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--auto", type=int, metavar="N", help="acha N highlights por pico de áudio")
    _common(p)
    p.set_defaults(func=cmd_vod)

    p = sub.add_parser("live", help="live ao vivo (link) -> buffer circular")
    p.add_argument("url")
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("live-clip", help="corta os últimos segundos do buffer da live")
    p.add_argument("--last", type=float, default=45, metavar="SEGUNDOS")
    _common(p)
    p.set_defaults(func=cmd_live_clip)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
