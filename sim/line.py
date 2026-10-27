"""Line simulator = the 'plant' + a remote I/O module on Modbus TCP.

The PLC (OpenPLC) polls this module like real remote I/O (WAGO, ET200...):
  discrete inputs 0..15 -> PLC %IX100.0 .. %IX101.7   (sensors, push buttons)
  coils 0..7            <- PLC %QX100.0 .. %QX100.7   (motor, ejector, lamps, trigger)
  input register 0      -> PLC %IW100                 (encoder counter module)

Physics: a belt with apples spaced one pitch apart, an encoder pulse per pitch,
photo-eyes at entry / camera / ejector, a camera station that grades each apple
with the apple-quality classifier, and an ejector that pushes rejects off the belt.
Ground truth is kept for every apple so the PLC's sorting can be scored.
"""
import queue
import random
import threading
import time
from dataclasses import dataclass, field

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext, ModbusSlaveContext  # pymodbus 3.8
from pymodbus.server import StartTcpServer

# --- discrete input / coil indexes (must match plc/sorting_line.st) ---
DI = dict(START=0, STOP=1, ESTOP_OK=2, PE_ENTRY=3, PE_VISION=4, VISION_DONE=5, VISION_BAD=6,
          ENCODER=7, RESET=8, PE_EJECT=9)
CO = dict(CONVEYOR=0, VISION_TRIG=1, EJECTOR=2, LAMP_GREEN=3, LAMP_AMBER=4, LAMP_RED=5)

PITCH = 0.25            # m between apples
SPEED = 0.25            # m/s  -> 1 apple per second
CAMERA_SLOT = 0.50      # m, where the encoder pulse finds an apple under the camera
PE_VISION_X = CAMERA_SLOT - PITCH / 2   # camera trigger eye, half a pitch early
EJECT_X = CAMERA_SLOT + 2 * PITCH       # 3rd encoder pulse after the trigger
PE_ENTRY_X = 0.05
BELT_END = 1.75
PART_HALF = 0.04        # apple half-width on the belt
PULSE_S = 0.06          # encoder / done pulse width (longer than the Modbus poll period)


@dataclass
class Apple:
    x: float
    bad: bool
    image: str
    verdict: object = None      # what the camera said (None = not inspected)
    fate: str = ""              # "good bin", "reject bin", "stuck"
    stuck: bool = False
    latency: float = None       # camera trigger to result, s
    t_done: float = 0.0


@dataclass
class Line:
    grader: object = None                       # callable(image_path) -> (is_bad, p_bad)
    images: list = field(default_factory=list)  # (path, is_bad)
    vision_fail: bool = False                   # camera never answers (fault injection)
    feed: bool = True

    def __post_init__(self):
        self.lock = threading.Lock()
        self.di = [False] * 16
        self.co = [False] * 8
        self.apples, self.done = [], []
        self.dist = 0.0
        self.enc_count = 0                      # counter module value (input register 0)
        self.enc_until = self.done_until = 0.0
        self.di[DI["ESTOP_OK"]] = True
        self._trig_prev = False
        self.t0 = time.time()
        self.events = []
        # camera station: one persistent worker, like a vision PC that stays warm
        self.jobs = queue.Queue()
        self.camera_ready = False
        threading.Thread(target=self._camera_worker, daemon=True).start()

    # ----- operator / fault injection -----
    def press(self, name, hold=0.3):
        def _p():
            with self.lock: self.di[DI[name]] = True
            time.sleep(hold)
            with self.lock: self.di[DI[name]] = False
        threading.Thread(target=_p, daemon=True).start()

    def estop(self, pressed):
        with self.lock: self.di[DI["ESTOP_OK"]] = not pressed
        self.log("E-STOP pressed" if pressed else "E-STOP released")

    def jam_next(self):
        """The next apple reaching the ejector gets stuck there."""
        self._jam = True

    def clear_jam(self):
        with self.lock:
            for a in self.apples:
                if a.stuck:
                    a.stuck, a.fate = False, "cleared by operator"
                    self.done.append(a)
            self.apples = [a for a in self.apples if a.fate != "cleared by operator"]
        self.log("jam cleared by operator")

    def log(self, msg):
        self.events.append((time.time() - self.t0, msg))

    # ----- physics step -----
    def step(self, dt):
        now = time.time()
        with self.lock:
            running = self.co[CO["CONVEYOR"]]
            if running:
                d = SPEED * dt
                before = int(self.dist / PITCH)
                self.dist += d
                for a in self.apples:
                    if not a.stuck:
                        a.x += d
                if int(self.dist / PITCH) > before:              # one pitch travelled
                    self.enc_until = now + PULSE_S
                    self.enc_count = (self.enc_count + 1) & 0xFFFF
                    if self.feed:
                        path, bad = random.choice(self.images)
                        self.apples.append(Apple(0.0, bad, path))
            # jam injection: stop an apple at the ejector eye
            if getattr(self, "_jam", False):
                for a in self.apples:
                    if abs(a.x - EJECT_X) < 0.01:
                        a.stuck, self._jam = True, False
                        self.log("apple jammed at ejector")
            # ejector: pushes the apple in front of it into the reject bin
            if self.co[CO["EJECTOR"]]:
                for a in self.apples:
                    if abs(a.x - EJECT_X) < PART_HALF + 0.02 and not a.stuck:
                        a.fate, a.t_done = "reject bin", now - self.t0
                        self.done.append(a)
                self.apples = [a for a in self.apples if not a.fate]
            for a in self.apples:
                if a.x > BELT_END:
                    a.fate, a.t_done = "good bin", now - self.t0; self.done.append(a)
            self.apples = [a for a in self.apples if not a.fate]
            # sensors
            eye = lambda x: any(abs(a.x - x) < PART_HALF for a in self.apples)
            self.di[DI["PE_ENTRY"]] = eye(PE_ENTRY_X)
            self.di[DI["PE_VISION"]] = eye(PE_VISION_X)
            self.di[DI["PE_EJECT"]] = eye(EJECT_X)
            self.di[DI["ENCODER"]] = now < self.enc_until
            self.di[DI["VISION_DONE"]] = now < self.done_until
            trig = self.co[CO["VISION_TRIG"]]
            fire = trig and not self._trig_prev
            self._trig_prev = trig
            target = next((a for a in self.apples if abs(a.x - PE_VISION_X) < PART_HALF), None) if fire else None
        if fire and target is not None and not self.vision_fail:
            self.jobs.put(target)

    def _camera_worker(self):
        for path, _ in self.images[:3]:          # warm up at boot: the first inferences are slow
            self.grader(path)
        self.camera_ready = True
        while True:
            self._inspect(self.jobs.get())

    def _inspect(self, apple):
        t = time.time()
        bad, p = self.grader(apple.image)
        with self.lock:
            apple.latency = time.time() - t
            apple.verdict = bad
            self.di[DI["VISION_BAD"]] = bad
            self.done_until = time.time() + PULSE_S

    # ----- Modbus remote I/O -----
    def serve(self, port=5020):
        # pymodbus 3.8 classic datastore: the slave context adds 1 to protocol addresses,
        # so the blocks start at 0 and hold one extra entry
        dev = ModbusSlaveContext(di=ModbusSequentialDataBlock(0, [0] * 17),
                                 co=ModbusSequentialDataBlock(0, [0] * 9),
                                 ir=ModbusSequentialDataBlock(0, [0] * 2))
        self.ctx = ModbusServerContext(slaves=dev, single=True)

        def sync():
            while True:
                with self.lock:
                    dev.setValues(2, 0, [int(v) for v in self.di])            # discrete inputs (FC2)
                    dev.setValues(4, 0, [self.enc_count])                     # encoder count (FC4)
                    self.co = [bool(v) for v in dev.getValues(1, 0, 8)]       # coils written by the PLC (FC1)
                time.sleep(0.002)
        threading.Thread(target=sync, daemon=True).start()
        threading.Thread(target=lambda: StartTcpServer(context=self.ctx, address=("0.0.0.0", port)), daemon=True).start()

    def run_physics(self, hz=200):
        def loop():
            last = time.time()
            while True:
                now = time.time(); self.step(now - last); last = now
                time.sleep(1 / hz)
        threading.Thread(target=loop, daemon=True).start()

    def score(self):
        d = self.done
        ok = sum((a.fate == "reject bin") == a.bad for a in d if a.fate in ("good bin", "reject bin"))
        escaped = sum(a.bad and a.fate == "good bin" for a in d)
        false_rej = sum((not a.bad) and a.fate == "reject bin" for a in d)
        failsafe = sum((not a.bad) and a.fate == "reject bin" and a.verdict is None for a in d)
        return dict(finished=len(d), correct=ok, bad_escaped=escaped, good_rejected=false_rej,
                    good_rejected_failsafe=failsafe, good_rejected_by_classifier=false_rej - failsafe)
