# Task 7.1: End-to-End Test Scenarios

## Objective

Validate complete user journeys through the system from all user perspectives.

## Dependencies

- All previous phases completed
- Test environment configured

---

## Test Scenarios

### Scenario 1: Complete Happy Path

**Objective:** Validate the complete flow from machine setup to patient viewing their report.

**Steps:**

1. [ ] Admin creates a gestionnaire account
2. [ ] Gestionnaire logs in, creates a machine
3. [ ] Gestionnaire copies API key
4. [ ] Configure Raspberry Pi with API key
5. [ ] Start RPi service, verify machine comes online
6. [ ] Gestionnaire creates a patient account
7. [ ] Technician (or gestionnaire) starts a session
8. [ ] RPi picks up session, starts recording
9. [ ] Verify live ECG data appears in dashboard
10. [ ] Gestionnaire views live feed (verify 5s delay)
11. [ ] Wait 5 minutes for sufficient data
12. [ ] End session via web UI
13. [ ] Verify RPi stops recording
14. [ ] Wait for summary generation
15. [ ] Verify heart rate metrics calculated
16. [ ] Generate PDF report
17. [ ] Patient logs in
18. [ ] Patient views their session
19. [ ] Patient downloads PDF report
20. [ ] Verify PDF contains correct data

**Expected Result:** All steps complete successfully, data flows correctly through system.

---

### Scenario 2: Offline Recovery

**Objective:** Validate data is not lost when RPi loses network connection.

**Steps:**

1. [ ] Start a session
2. [ ] Verify data streaming normally
3. [ ] Disconnect RPi from network (unplug ethernet/disable WiFi)
4. [ ] Continue recording for 5 minutes
5. [ ] Verify RPi buffers data locally
6. [ ] Reconnect network
7. [ ] Verify buffered data syncs to server
8. [ ] End session
9. [ ] Verify all data present in summary
10. [ ] Verify no data gaps

**Expected Result:** No data lost, all buffered data syncs correctly.

---

### Scenario 3: Machine Failure

**Objective:** Validate graceful handling when BITalino disconnects.

**Steps:**

1. [ ] Start a session
2. [ ] Record for 2 minutes
3. [ ] Disconnect BITalino (simulate failure)
4. [ ] Verify RPi detects disconnect
5. [ ] Verify session marked as failed
6. [ ] Verify partial data preserved
7. [ ] Verify machine status updates to offline
8. [ ] Reconnect BITalino
9. [ ] Create new session
10. [ ] Verify new session works normally

**Expected Result:** Failure handled gracefully, partial data preserved, recovery works.

---

### Scenario 4: Heartbeat Timeout

**Objective:** Validate machine goes offline when heartbeats stop.

**Steps:**

1. [ ] Verify machine is online
2. [ ] Stop RPi service
3. [ ] Wait 90 seconds
4. [ ] Check machine status → should be offline
5. [ ] Start RPi service
6. [ ] Wait for heartbeat
7. [ ] Verify machine status → online

**Expected Result:** Offline detection works, recovery automatic.

---

### Scenario 5: Role-Based Access Control

**Objective:** Validate users can only access appropriate data.

**Steps:**

1. [ ] Create two gestionnaires (G1, G2)
2. [ ] G1 creates machine M1 and patient P1
3. [ ] G2 creates machine M2 and patient P2
4. [ ] G1 tries to view M2 → should fail
5. [ ] G1 tries to view P2's sessions → should fail
6. [ ] G2 tries to start session on M1 → should fail
7. [ ] Admin views all machines → should succeed
8. [ ] Admin views all sessions → should succeed
9. [ ] P1 tries to view P2's session → should fail
10. [ ] P1 views own session → should succeed

**Expected Result:** Access control properly enforced for all roles.

---

### Scenario 6: Multi-Language Support

**Objective:** Validate i18n works correctly.

**Steps:**

1. [ ] Load app in French (/fr/dashboard)
2. [ ] Verify all UI text in French
3. [ ] Switch to English
4. [ ] Verify URL changes to /en/dashboard
5. [ ] Verify all UI text in English
6. [ ] Create patient with French language preference
7. [ ] Login as patient
8. [ ] Verify default language is French
9. [ ] Change language preference in profile
10. [ ] Verify language persists

**Expected Result:** All text translated, language preferences work.

---

### Scenario 7: Concurrent Sessions

**Objective:** Validate multiple sessions can run simultaneously on different machines.

**Steps:**

1. [ ] Setup two RPis (M1, M2)
2. [ ] Create two patients (P1, P2)
3. [ ] Start session for P1 on M1
4. [ ] Start session for P2 on M2
5. [ ] Verify both streaming data
6. [ ] View live feed for both sessions
7. [ ] End session on M1
8. [ ] Verify M2 continues unaffected
9. [ ] End session on M2
10. [ ] Verify both summaries generated

**Expected Result:** Sessions independent, no interference.

---

## Test Checklist

| Scenario               | Status | Notes |
| ---------------------- | ------ | ----- |
| 1. Happy Path          | [ ]    |       |
| 2. Offline Recovery    | [ ]    |       |
| 3. Machine Failure     | [ ]    |       |
| 4. Heartbeat Timeout   | [ ]    |       |
| 5. RBAC                | [ ]    |       |
| 6. Multi-Language      | [ ]    |       |
| 7. Concurrent Sessions | [ ]    |       |

---

## Known Issues to Verify Fixed

- [ ] Session doesn't auto-start if created before RPi connects
- [ ] Buffered data timestamp drift after long offline period
- [ ] PDF generation fails for very short sessions
- [ ] Language switcher resets scroll position
