/* The waiter sees an unsynchronized kernel without changing the shared clock.
 * Only the drill's systemd-time-wait-sync process preloads this fixture (docs/deploy.md).
 */
#include <sys/timex.h>

int adjtimex(struct timex *tx) {
    tx->status = STA_UNSYNC;
    return TIME_ERROR;
}
