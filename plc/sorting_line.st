(*
  Smart sorting line: PLC program (IEC 61131-3 Structured Text, OpenPLC)

  A conveyor carries apples past a vision station. Each part is tracked by a
  shift register clocked by the belt encoder, and bad parts are pushed off the
  belt by a pneumatic ejector further downstream.

  I/O lives on a remote I/O module (the line simulator) polled over Modbus TCP:
    %IX100.x / %QX100.x = discrete inputs / coils of the remote I/O
    %MW0..   = status words for the HMI (Modbus holding registers 1024..)

  Safety and fault handling:
    * E-stop chain (normally closed) stops the belt at once; restart needs RESET
    * jam detection: a photo-eye blocked for too long while running
    * vision timeout: no result after the trigger -> part is rejected (fail-safe)
*)
PROGRAM SortingLine
  VAR
    (* ---- inputs from remote I/O ---- *)
    START_PB      AT %IX100.0 : BOOL;
    STOP_PB       AT %IX100.1 : BOOL;
    ESTOP_OK      AT %IX100.2 : BOOL;   (* TRUE = safety chain healthy *)
    PE_ENTRY      AT %IX100.3 : BOOL;   (* photo-eye at belt entry *)
    PE_VISION     AT %IX100.4 : BOOL;   (* part under the camera *)
    VISION_DONE   AT %IX100.5 : BOOL;   (* camera finished *)
    VISION_BAD    AT %IX100.6 : BOOL;   (* camera verdict, valid when DONE *)
    ENCODER       AT %IX100.7 : BOOL;   (* raw encoder pulse, for diagnostics only *)
    RESET_PB      AT %IX101.0 : BOOL;
    PE_EJECT      AT %IX101.1 : BOOL;   (* photo-eye at ejector *)
    ENC_COUNT     AT %IW100 : UINT;     (* counter module: belt pitches, wraps at 65535 *)

    (* ---- outputs to remote I/O ---- *)
    CONVEYOR      AT %QX100.0 : BOOL;
    VISION_TRIG   AT %QX100.1 : BOOL;
    EJECTOR       AT %QX100.2 : BOOL;
    LAMP_GREEN    AT %QX100.3 : BOOL;
    LAMP_AMBER    AT %QX100.4 : BOOL;
    LAMP_RED      AT %QX100.5 : BOOL;

    (* ---- status for the HMI ---- *)
    HMI_STATE     AT %MW0 : INT;   (* 0 idle, 1 running, 2 stopped, 3 e-stop, 4 jam *)
    HMI_GOOD      AT %MW1 : INT;
    HMI_BAD       AT %MW2 : INT;
    HMI_TOTAL     AT %MW3 : INT;
    HMI_V_TIMEOUT AT %MW4 : INT;   (* vision timeouts (parts rejected fail-safe) *)
    HMI_JAMS      AT %MW5 : INT;
    HMI_EJECTS    AT %MW6 : INT;
  END_VAR

  VAR
    (* ---- internal (matiec wants these in their own VAR block) ---- *)
    State      : INT := 0;
    Track      : ARRAY[0..5] OF BOOL;   (* TRUE = reject, index 0 = vision position *)
    i          : INT;
    EJECT_SLOT : INT := 3;              (* ejector is 3 pitches after the camera *)
    WaitResult : BOOL := FALSE;
    LastCount  : UINT := 0;
    Steps      : UINT := 0;
    Synced     : BOOL := FALSE;
    Fire       : BOOL := FALSE;

    StartEdge  : R_TRIG;
    VisEdge    : R_TRIG;
    DoneEdge   : R_TRIG;
    JamTimer   : TON;
    VisTimer   : TON;
    EjectPulse : TP;
  END_VAR

  StartEdge(CLK := START_PB);
  VisEdge(CLK := PE_VISION);
  DoneEdge(CLK := VISION_DONE);

  (* ===================== state machine ===================== *)
  IF NOT ESTOP_OK THEN
    State := 3;                                   (* e-stop always wins *)
  END_IF;

  CASE State OF
    0, 2:                                          (* idle / stopped *)
      IF StartEdge.Q AND ESTOP_OK THEN
        State := 1;
      END_IF;
    1:                                             (* running *)
      IF STOP_PB THEN
        State := 2;
      ELSIF JamTimer.Q THEN
        State := 4;
        HMI_JAMS := HMI_JAMS + 1;
      END_IF;
    3:                                             (* e-stop: wait for healthy chain + reset *)
      IF ESTOP_OK AND RESET_PB THEN
        State := 2;
      END_IF;
    4:                                             (* jam: operator clears it, then reset.   *)
      IF RESET_PB THEN                             (* an eye may still see a normal part on   *)
        State := 2;                                (* the stopped belt; if the jam is still   *)
      END_IF;                                      (* there the 3 s timer trips again on run  *)
  END_CASE;

  CONVEYOR := (State = 1);

  (* jam: entry or ejector eye blocked for more than 3 s while the belt runs *)
  JamTimer(IN := CONVEYOR AND (PE_ENTRY OR PE_EJECT), PT := T#3s);

  (* ===================== vision handshake ===================== *)
  IF VisEdge.Q AND CONVEYOR THEN
    VISION_TRIG := TRUE;
    WaitResult := TRUE;
  END_IF;

  (* the camera is triggered half a pitch before the part reaches the camera slot  *)
  (* (500 ms at 1 part/s). A later answer would land on the wrong part, so after   *)
  (* 400 ms the part is treated as unknown and rejected.                            *)
  VisTimer(IN := WaitResult, PT := T#400ms);

  IF WaitResult AND DoneEdge.Q THEN
    Track[0] := VISION_BAD;                        (* store verdict at camera slot *)
    WaitResult := FALSE;
    VISION_TRIG := FALSE;
    HMI_TOTAL := HMI_TOTAL + 1;
    IF VISION_BAD THEN HMI_BAD := HMI_BAD + 1; ELSE HMI_GOOD := HMI_GOOD + 1; END_IF;
  ELSIF WaitResult AND VisTimer.Q THEN
    Track[0] := TRUE;                              (* fail-safe: unknown part is rejected *)
    WaitResult := FALSE;
    VISION_TRIG := FALSE;
    HMI_TOTAL := HMI_TOTAL + 1;
    HMI_BAD := HMI_BAD + 1;
    HMI_V_TIMEOUT := HMI_V_TIMEOUT + 1;
  END_IF;

  (* ===================== part tracking ===================== *)
  (* The encoder is read as a count from a counter module, not as a pulse: a      *)
  (* polled pulse can be missed when the I/O scan is late, and one missed pulse    *)
  (* shifts every tracked part by one slot.                                        *)
  Steps := ENC_COUNT - LastCount;                  (* UINT arithmetic handles wrap *)
  IF NOT Synced OR Steps > 5 THEN                  (* first scan or I/O reconnect *)
    LastCount := ENC_COUNT;
    Steps := 0;
    Synced := TRUE;
  END_IF;
  Fire := FALSE;
  WHILE Steps > 0 DO
    FOR i := 5 TO 1 BY -1 DO
      Track[i] := Track[i - 1];
    END_FOR;
    Track[0] := FALSE;
    IF Track[EJECT_SLOT] THEN                      (* a reject just reached the ejector *)
      Fire := TRUE;
      HMI_EJECTS := HMI_EJECTS + 1;
    END_IF;
    LastCount := LastCount + 1;
    Steps := Steps - 1;
  END_WHILE;

  EjectPulse(IN := Fire, PT := T#250ms);
  EJECTOR := EjectPulse.Q;

  (* ===================== lamps ===================== *)
  LAMP_GREEN := (State = 1);
  LAMP_AMBER := (State = 0) OR (State = 2);
  LAMP_RED   := (State = 3) OR (State = 4);
  HMI_STATE  := State;
END_PROGRAM


CONFIGURATION Config0
  RESOURCE Res0 ON PLC
    TASK MainTask(INTERVAL := T#10ms, PRIORITY := 0);
    PROGRAM Inst0 WITH MainTask : SortingLine;
  END_RESOURCE
END_CONFIGURATION
