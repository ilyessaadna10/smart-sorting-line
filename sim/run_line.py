#!/usr/bin/env python3
"""Run the smart sorting line: plant + remote I/O + vision station + HMI recording.

The PLC itself runs in OpenPLC (plc/sorting_line.st). This script:
  1. starts the line simulator and its Modbus TCP remote I/O (port 5020),
  2. grades each apple with the apple-quality classifier (fast mode),
  3. plays a scenario (start, production, E-stop, jam, camera failure),
  4. reads the PLC status words from OpenPLC's Modbus server (port 5502 here),
  5. draws an HMI and records it to results/line_demo.mp4,
  6. checks the PLC's reactions and writes results/fat_report.md.

    python run_line.py --models /path/to/convnext_folds --images /path/to/apple_cutouts
"""
import argparse
import glob
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from pymodbus.client import ModbusTcpClient

sys.path.insert(0, str(Path(__file__).parent))
from line import CO, DI, EJECT_X, PE_ENTRY_X, PE_VISION_X, CAMERA_SLOT, BELT_END, Line

ROOT = Path(__file__).resolve().parents[1]
STATES = {0: "IDLE", 1: "RUNNING", 2: "STOPPED", 3: "E-STOP", 4: "JAM"}


def make_grader(model_dir):
    """Apple classifier in fast mode (1 model, 1 view): about 33 ms per apple."""
    sys.path.insert(0, str(ROOT.parent / "apple-quality-classifier" / "inference"))
    from predict import AppleGrader, load_rgb_alpha, to_canvas
    g = AppleGrader(model_dir, tta=False)
    g.models = g.models[:1]
    def grade(path):
        p = float(g.p_bad([to_canvas(*load_rgb_alpha(path))])[0])
        return p >= 0.5, p
    return grade


def load_images(folder):
    imgs = [(p, "bad" in Path(p).parent.name.lower()) for p in glob.glob(f"{folder}/**/*.png", recursive=True)]
    if not imgs:
        raise SystemExit(f"no apple images in {folder}")
    return imgs


class HMI:
    """Operator screen: belt view, lamps, PLC status and production KPIs."""
    W, H = 1100, 560

    def __init__(self, line, images):
        self.line = line
        self.thumbs = {}
        for p, _ in images:
            im = cv2.imread(p, cv2.IMREAD_UNCHANGED)
            if im is not None and im.shape[2] == 4:
                a = im[:, :, 3:] / 255.0
                im = (im[:, :, :3] * a + 60 * (1 - a)).astype(np.uint8)
            self.thumbs[p] = cv2.resize(im[:, :, :3], (54, 54))

    def x2px(self, x):
        return int(60 + x / BELT_END * 900)

    def draw(self, plc, t):
        img = np.full((self.H, self.W, 3), 32, np.uint8)
        ln = self.line
        with ln.lock:
            apples = [(a.x, a.image, a.verdict, a.bad) for a in ln.apples]
            co, di = list(ln.co), list(ln.di)
            done = list(ln.done)
        # belt
        cv2.rectangle(img, (55, 250), (965, 320), (70, 70, 70), -1)
        for k in range(0, 900, 30):
            off = int((ln.dist * 900 / BELT_END) % 30)
            cv2.line(img, (60 + k + off, 250), (60 + k + off, 320), (90, 90, 90), 1)
        # stations
        for x, name, active in ((PE_ENTRY_X, "entry eye", di[DI["PE_ENTRY"]]), (PE_VISION_X, "camera trigger", di[DI["PE_VISION"]]),
                                (EJECT_X, "ejector eye", di[DI["PE_EJECT"]])):
            px = self.x2px(x)
            cv2.line(img, (px, 230), (px, 340), (0, 200, 255) if active else (0, 90, 120), 2)
            cv2.putText(img, name, (px - 40, 360), 0, 0.42, (200, 200, 200), 1)
        cam = self.x2px(CAMERA_SLOT)
        cv2.rectangle(img, (cam - 30, 170), (cam + 30, 210), (0, 255, 0) if co[CO["VISION_TRIG"]] else (90, 90, 90), 2)
        cv2.putText(img, "CAMERA", (cam - 30, 165), 0, 0.45, (220, 220, 220), 1)
        ej = self.x2px(EJECT_X)
        cv2.rectangle(img, (ej - 18, 200 if not co[CO["EJECTOR"]] else 265), (ej + 18, 245 if not co[CO["EJECTOR"]] else 300),
                      (0, 0, 255) if co[CO["EJECTOR"]] else (110, 110, 110), -1)
        cv2.putText(img, "EJECTOR", (ej - 32, 195), 0, 0.45, (220, 220, 220), 1)
        if co[CO["EJECTOR"]]:
            cv2.arrowedLine(img, (ej, 330), (ej, 395), (0, 0, 255), 3, tipLength=0.3)
            cv2.putText(img, "to reject bin", (ej + 8, 392), 0, 0.45, (0, 0, 255), 1)
        for x, path, verdict, bad in apples:
            px = self.x2px(x)
            if 0 <= px - 27 and px + 27 < self.W:
                img[258:312, px - 27:px + 27] = self.thumbs[path]
                if verdict is not None:
                    cv2.rectangle(img, (px - 28, 257), (px + 28, 313), (0, 0, 255) if verdict else (0, 220, 0), 2)
        # lamps
        for k, (name, col) in enumerate((("LAMP_GREEN", (0, 220, 0)), ("LAMP_AMBER", (0, 190, 255)), ("LAMP_RED", (0, 0, 255)))):
            cv2.circle(img, (1010, 120 + 45 * k), 16, col if co[CO[name]] else (60, 60, 60), -1)
            cv2.putText(img, ("RUN", "READY", "FAULT")[k], (1034, 126 + 45 * k), 0, 0.45, (200, 200, 200), 1)
        # PLC status + KPIs
        state = STATES.get(plc.get("state", -1), "?")
        cv2.putText(img, f"PLC state: {state}", (60, 50), 0, 0.9, (255, 255, 255), 2)
        cv2.putText(img, f"t = {t:5.1f} s", (60, 85), 0, 0.6, (200, 200, 200), 1)
        good, bad, total = plc.get("good", 0), plc.get("bad", 0), plc.get("total", 0)
        rej = sum(a.fate == "reject bin" for a in done); passed = sum(a.fate == "good bin" for a in done)
        wrong = sum((a.fate == "reject bin") != a.bad for a in done if a.fate in ("good bin", "reject bin"))
        lines = [f"Inspected (PLC): {total}   good {good}   bad {bad}",
                 f"Vision timeouts: {plc.get('vto', 0)}   Jams: {plc.get('jams', 0)}   Ejections: {plc.get('ejects', 0)}",
                 f"Good bin: {passed}   Reject bin: {rej}   Missorted vs ground truth: {wrong}"]
        for k, s in enumerate(lines):
            cv2.putText(img, s, (60, 420 + 30 * k), 0, 0.6, (230, 230, 230), 1)
        for k, (tt, msg) in enumerate(ln.events[-4:]):
            cv2.putText(img, f"{tt:5.1f}s  {msg}", (600, 420 + 25 * k), 0, 0.5, (0, 200, 255), 1)
        return img


def wait_for_static(cond, timeout):
    t = time.time()
    while time.time() - t < timeout:
        if cond(): return True
        time.sleep(0.05)
    return False


def read_plc(client):
    r = client.read_holding_registers(1024, count=7)
    if r.isError():
        return {}
    v = r.registers
    return dict(state=v[0], good=v[1], bad=v[2], total=v[3], vto=v[4], jams=v[5], ejects=v[6])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True)
    ap.add_argument("--images", required=True)
    ap.add_argument("--plc", default="127.0.0.1")
    ap.add_argument("--plc-port", type=int, default=502)
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    images = load_images(args.images)
    line = Line(grader=make_grader(args.models), images=images)
    line.serve(5020)
    line.run_physics()
    while not line.camera_ready:
        time.sleep(0.1)
    plc = ModbusTcpClient(args.plc, port=args.plc_port)
    # wait until the PLC is up and polling the remote I/O (it drives a lamp in every state)
    t_wait = time.time()
    while not (plc.connect() and read_plc(plc) and (line.co[CO["LAMP_AMBER"]] or line.co[CO["LAMP_RED"]])):
        if time.time() - t_wait > 60:
            raise SystemExit("PLC not reachable or not polling the remote I/O on port 5020")
        time.sleep(0.5)
    print(f"PLC online and polling after {time.time() - t_wait:.1f} s")
    checks = []
    # while the remote I/O was offline the PLC saw ESTOP_OK = 0, so it must sit in E-stop until RESET
    if read_plc(plc).get("state") == 3:
        line.press("RESET")
        ok = wait_for_static(lambda: read_plc(plc).get("state") == 2, 3.0)
        checks.append(("I/O loss at power up is treated as E-stop, cleared only by RESET", ok, "PLC held E-stop until RESET"))
    hmi = HMI(line, images)
    out_dir = ROOT / "results"; out_dir.mkdir(exist_ok=True)
    vid = cv2.VideoWriter(str(out_dir / "line_demo.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), 20, (HMI.W, HMI.H))

    trace = []
    def wait_for(cond, timeout, step=0.01):
        t = time.time()
        while time.time() - t < timeout:
            if cond(): return time.time() - t
            time.sleep(step)
        return None

    # scenario: (time, action)
    script = [
        (1.0, "start"), (30.0, "estop"), (33.0, "estop_release"), (34.0, "reset"), (35.0, "start"),
        (45.0, "jam"), (55.0, "clear_jam"), (56.0, "reset"), (57.0, "start"),
        (67.0, "vision_fail"), (73.0, "vision_ok"), (85.0, "stop"),
    ]
    t0 = time.time(); k = 0; frame_t = 0.0
    line.t0 = t0                                    # event log and video share one clock
    while time.time() - t0 < 90:
        t = time.time() - t0
        while k < len(script) and t >= script[k][0]:
            act = script[k][1]; k += 1
            if act in ("start", "stop", "reset"):
                line.press(act.upper()); line.log(f"operator: {act.upper()}")
            elif act == "estop":
                line.estop(True)
                dt = wait_for(lambda: not line.co[CO["CONVEYOR"]], 1.0)
                checks.append(("E-stop stops the belt", dt is not None, f"belt off after {dt*1000:.0f} ms" if dt is not None else "belt still running"))
            elif act == "estop_release":
                line.estop(False)
                time.sleep(0.5)
                checks.append(("No restart without RESET after E-stop", not line.co[CO["CONVEYOR"]], "belt stays off until reset"))
            elif act == "jam":
                line.jam_next(); line.log("fault injected: jam")
                dt = wait_for(lambda: read_plc(plc).get("state") == 4, 8.0, 0.05)
                checks.append(("Jam detected and belt stopped", dt is not None, f"jam state after {dt:.1f} s" if dt else "not detected"))
            elif act == "clear_jam":
                line.clear_jam()
            elif act == "vision_fail":
                line.vision_fail = True; line.log("fault injected: camera offline")
                before = read_plc(plc).get("vto", 0)
            elif act == "vision_ok":
                line.vision_fail = False; line.log("camera back online")
                after = read_plc(plc).get("vto", 0)
                checks.append(("Camera failure: parts rejected fail-safe", after > before, f"{after - before} parts rejected on timeout"))
        if t >= frame_t:
            s = read_plc(plc); trace.append((t, s))
            frame = hmi.draw(s, t); vid.write(frame); frame_t += 0.05
            if args.show:
                cv2.imshow("Smart sorting line HMI", frame); cv2.waitKey(1)
        time.sleep(0.005)
    vid.release()

    sc = line.score()
    good_inspected = [a for a in line.done if a.verdict is not None and a.fate in ("good bin", "reject bin")]
    routed_ok = sum((a.fate == "reject bin") == a.verdict for a in good_inspected)
    checks.append(("Every inspected apple routed as the camera decided", routed_ok == len(good_inspected),
                   f"{routed_ok}/{len(good_inspected)} apples followed the camera verdict"))
    lat = np.array([a.latency for a in line.done + line.apples if a.latency is not None]) * 1000
    plc_end = read_plc(plc)
    with open(out_dir / "fat_report.md", "w") as f:
        f.write("# Factory acceptance test (simulated line, real OpenPLC runtime)\n\n")
        f.write("| Check | Result | Detail |\n|---|---|---|\n")
        for name, ok, detail in checks:
            f.write(f"| {name} | {'PASS' if ok else 'FAIL'} | {detail} |\n")
        f.write("\n## Production over the run\n\n| | |\n|---|---|\n")
        rows = [("Apples finished", sc["finished"]), ("Inspected by the PLC", plc_end.get("total")),
                ("Bad apples that reached the good bin", sc["bad_escaped"]),
                ("Good apples rejected because the camera was offline (fail-safe)", sc["good_rejected_failsafe"]),
                ("Good apples rejected by a classifier mistake", sc["good_rejected_by_classifier"]),
                ("Vision timeouts / jams / ejections (PLC counters)",
                 f"{plc_end.get('vto')} / {plc_end.get('jams')} / {plc_end.get('ejects')}")]
        if len(lat):
            rows.append(("Camera latency, trigger to result (ms)",
                         f"p50 {np.median(lat):.0f}, p95 {np.percentile(lat, 95):.0f}, max {lat.max():.0f} (budget 400)"))
        for k, v in rows:
            f.write(f"| {k} | {v} |\n")
        f.write("\n## Event log\n\n")
        for tt, msg in line.events:
            f.write(f"- {tt:5.1f} s: {msg}\n")
    print(open(out_dir / "fat_report.md").read())


if __name__ == "__main__":
    main()
