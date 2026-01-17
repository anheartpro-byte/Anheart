# Task 7.2: Performance Testing

## Objective

Validate system performance under expected and peak loads.

## Dependencies

- Task 7.1 completed (basic functionality working)

---

## Performance Requirements

| Metric                          | Target       | Max Acceptable |
| ------------------------------- | ------------ | -------------- |
| Data latency (RPi to dashboard) | < 2 seconds  | < 5 seconds    |
| Heartbeat processing            | < 100ms      | < 500ms        |
| Session creation                | < 500ms      | < 2 seconds    |
| Summary generation (1h session) | < 30 seconds | < 60 seconds   |
| PDF generation                  | < 10 seconds | < 30 seconds   |
| Dashboard page load             | < 1 second   | < 3 seconds    |
| Live ECG update rate            | 1 Hz         | 0.5 Hz         |

---

## Test Scenarios

### Test 1: Sustained Data Rate

**Objective:** Verify RPi can sustain 1000 samples/second for extended period.

**Steps:**

1. Start session
2. Record for 1 hour continuously
3. Monitor:
   - CPU usage on RPi
   - Memory usage on RPi
   - Network bandwidth
   - Data batches sent per minute
   - Error rate
4. End session

**Pass Criteria:**

- [ ] No data loss
- [ ] CPU < 50% average
- [ ] Memory stable (no growth)
- [ ] 60 batches/minute ±2
- [ ] Error rate < 0.1%

---

### Test 2: Summary Generation Speed

**Objective:** Verify summary generation completes in acceptable time.

**Test Matrix:**

| Session Duration | Data Points   | Target Time | Actual Time |
| ---------------- | ------------- | ----------- | ----------- |
| 5 minutes        | 300 batches   | < 5s        |             |
| 30 minutes       | 1,800 batches | < 15s       |             |
| 1 hour           | 3,600 batches | < 30s       |             |
| 2 hours          | 7,200 batches | < 60s       |             |

---

### Test 3: Concurrent Live Views

**Objective:** Verify dashboard handles multiple users viewing live ECG.

**Steps:**

1. Start active session
2. Open live view in 10 browser tabs
3. Monitor:
   - CPU usage (server-side via Convex dashboard)
   - Update rate in each tab
   - Memory usage in browser
4. Increase to 20 tabs
5. Note when performance degrades

**Pass Criteria:**

- [ ] 10 concurrent viewers: < 10% performance impact
- [ ] Update rate consistent across all viewers
- [ ] No browser tab crashes

---

### Test 4: Offline Buffer Capacity

**Objective:** Verify buffer can store expected offline duration.

**Steps:**

1. Calculate: 1 batch/second × 3600 seconds = 3,600 batches per hour
2. Each batch ~2KB → 7.2MB per hour
3. Start session, disconnect network
4. Record for 4 hours
5. Check buffer size
6. Reconnect and sync
7. Measure sync time

**Pass Criteria:**

- [ ] Buffer handles 4+ hours of data
- [ ] SQLite file < 100MB for 4 hours
- [ ] Sync completes in < 10 minutes for 4 hours of data

---

### Test 5: Database Query Performance

**Objective:** Verify Convex queries remain fast as data grows.

**Steps:**

1. Generate 100 completed sessions
2. Run typical queries:
   - List sessions (paginated)
   - Get session details
   - Get ECG data range
   - Get summary
3. Measure response times

**Pass Criteria:**

- [ ] List sessions: < 200ms
- [ ] Session details: < 100ms
- [ ] ECG data range (1 minute): < 500ms
- [ ] Summary: < 100ms

---

### Test 6: PDF Generation Load

**Objective:** Verify multiple PDF generations don't overload system.

**Steps:**

1. Complete 5 sessions
2. Trigger report generation for all 5 simultaneously
3. Monitor Convex function execution time
4. Verify all PDFs generated correctly

**Pass Criteria:**

- [ ] All PDFs generated
- [ ] No function timeouts
- [ ] Total time < 2 minutes for 5 reports

---

## Performance Monitoring Setup

### RPi Monitoring Script

```python
# raspberry-pi/scripts/monitor.py
import psutil
import time

while True:
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    print(f"CPU: {cpu}% | Memory: {mem.percent}% ({mem.used / 1024 / 1024:.1f}MB)")
    time.sleep(5)
```

### Browser Performance

- Use Chrome DevTools Performance tab
- Monitor memory usage
- Check for memory leaks over time

### Convex Monitoring

- Use Convex dashboard logs
- Monitor function execution times
- Check for rate limiting warnings

---

## Results Template

| Test                   | Date | Result    | Notes |
| ---------------------- | ---- | --------- | ----- |
| 1. Sustained Data Rate |      | Pass/Fail |       |
| 2. Summary Generation  |      | Pass/Fail |       |
| 3. Concurrent Views    |      | Pass/Fail |       |
| 4. Offline Buffer      |      | Pass/Fail |       |
| 5. Query Performance   |      | Pass/Fail |       |
| 6. PDF Generation      |      | Pass/Fail |       |

---

## Optimization Recommendations

If tests fail, consider:

1. **Data Rate Issues**
   - Increase batch size
   - Reduce sample rate
   - Optimize BLE reading

2. **Summary Generation Slow**
   - Process in chunks
   - Use Web Workers
   - Cache intermediate results

3. **Dashboard Performance**
   - Virtualize long lists
   - Lazy load components
   - Optimize re-renders

4. **Buffer Issues**
   - Use binary format instead of JSON
   - Compress data
   - Increase cleanup frequency
