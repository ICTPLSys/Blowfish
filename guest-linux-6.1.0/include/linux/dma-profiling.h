#ifndef _LINUX_DMA_PROFILING_H
#define _LINUX_DMA_PROFILING_H
#include <linux/rbtree.h>
#include <linux/kvm_host.h>
#include <linux/notifier.h>
#include <linux/reboot.h>
#include <linux/swap_stats.h>

#define LAT_PROFILING
// #define LLFREE_PROFILING
/* Toggle all [SWAP_DEBUG] logs in MM/swap paths. */
// #define ENABLE_SWAP_DEBUG
/* Force swap backends to use the THP/solidstate cluster init path. */
#define THP_SWAP_ALWAYS

#ifdef ENABLE_SWAP_DEBUG
#define SWAP_DEBUG_LOG(fmt, ...) printk("[SWAP_DEBUG] " fmt, ##__VA_ARGS__)
#else
#define SWAP_DEBUG_LOG(fmt, ...) do { } while (0)
#endif

/* Hypercall numbers for page reporting */
#define KVM_HC_REGISTER_PAGE_REPORTING_BUFFERS 37
#define KVM_HC_PROCESS_PAGE_REPORTING 38
#define KVM_HC_BLOWFISH_REGISTER_BUFFER 43
#define KVM_HC_BLOWFISH_PAGEOUT_RECLAIM 45
#define KVM_HC_BLOWFISH_ASYNC_RECLAIM_REGISTER 47

#define BLOWFISH_BUFFER_TYPE_RESTORE 0
#define BLOWFISH_BUFFER_TYPE_RECLAIM 1

#define BLOWFISH_RESTORE_BUFFER_ORDER 10 /* 2^10 pages */
/* Reclaim report buffer allocation order (2^5 pages = 32 pages). */
#define BLOWFISH_RECLAIM_BUFFER_ORDER 5

#define BLOWFISH_ASYNC_TOTAL_PAGES	1024UL
#define BLOWFISH_ASYNC_HEADER_BYTES	128UL
#define BLOWFISH_ASYNC_N_SLOTS()					\
	(unsigned int)(((BLOWFISH_ASYNC_TOTAL_PAGES << PAGE_SHIFT) - \
			BLOWFISH_ASYNC_HEADER_BYTES) / (2 * sizeof(u64)))

#define BLOWFISH_ASYNC_PAGE_ORDER	10 /* 2^10 == BLOWFISH_ASYNC_TOTAL_PAGES */

#ifdef CONFIG_BLOWFISH
extern u64 *restore_buffer;
extern size_t restore_buffer_size;
extern u64 *reclaim_gfn_buffer;
extern u32 *reclaim_len_buffer;
extern size_t reclaim_buffer_size;

int blowfish_restore_buffer_pop(u64 *pfn);
int blowfish_park_reported_folio(struct folio *folio);
bool blowfish_restore_reported_folio(unsigned long pfn);
int blowfish_cleanup_unmapped_reported_folios(void);
#endif

#endif /* _LINUX_DMA_PROFILING_H */