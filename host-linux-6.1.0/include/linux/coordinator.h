#ifndef __LINUX_COORDINATOR_H_
#define __LINUX_COORDINATOR_H_

#include <linux/printk.h>
#ifndef BLOWFISH_HOST_LOG
#define BLOWFISH_HOST_LOG 0
#endif
#if BLOWFISH_HOST_LOG
#define bf_host_pr_info(fmt, ...) pr_info(fmt, ##__VA_ARGS__)
#define bf_host_pr_warn(fmt, ...) pr_warn(fmt, ##__VA_ARGS__)
#else
#define bf_host_pr_info(fmt, ...) no_printk(fmt, ##__VA_ARGS__)
#define bf_host_pr_warn(fmt, ...) no_printk(fmt, ##__VA_ARGS__)
#endif

#include <linux/kernel.h>
#include <linux/kvm_para.h>
#include <linux/kvm_host.h>
#include <linux/kthread.h>
#include <linux/rbtree.h>
#include <linux/delay.h>
#include "linux/adc_timer.h"
#include <linux/highmem.h>
#include <linux/swap_stats.h>
#include <linux/frontswap.h>
#include <linux/swap.h>
#include <linux/swapops.h>
#include <linux/kvm_host.h>

// #define DEBUG_PROFILING
#define LAT_PROFILING
// #define ENABLE_SWAP_DEBUG_HOST
#define THP_SWAP_ALWAYS

#ifdef ENABLE_SWAP_DEBUG_HOST
#define SWAP_DEBUG_HOST_LOG(fmt, ...) printk("[SWAP_DEBUG_HOST] " fmt, ##__VA_ARGS__)
#else
#define SWAP_DEBUG_HOST_LOG(fmt, ...) do { } while (0)
#endif

/* Hypercall numbers for page reporting */
#define KVM_HC_REGISTER_PAGE_REPORTING_BUFFERS 37
#define KVM_HC_PROCESS_PAGE_REPORTING 38
#define KVM_HC_BLOWFISH_REGISTER_BUFFER 43
#define KVM_HC_THP_SWAP_GATE 44
#define KVM_HC_BLOWFISH_PAGEOUT_RECLAIM 45

#define KVM_HC_BLOWFISH_ASYNC_RECLAIM_REGISTER 47

#define BLOWFISH_BUFFER_TYPE_RESTORE 0
#define BLOWFISH_BUFFER_TYPE_RECLAIM 1
/* Must match guest reclaim buffer allocation order. */
#define BLOWFISH_RECLAIM_BUFFER_ORDER 5

#define BLOWFISH_ASYNC_TOTAL_PAGES	1024UL
#define BLOWFISH_ASYNC_HEADER_BYTES	128UL
#define BLOWFISH_ASYNC_N_SLOTS()						\
	(unsigned int)(((BLOWFISH_ASYNC_TOTAL_PAGES << PAGE_SHIFT) -	\
			BLOWFISH_ASYNC_HEADER_BYTES) / (2 * sizeof(u64)))

int commit_inflate_report(struct kvm* kvm);
int add_inflate_report(uint64_t pa, struct kvm* kvm, uint64_t len);
int do_deflate_report_range(struct kvm_vcpu *vcpu, uint64_t pa, uint64_t len);
int do_inflate_report_range(struct kvm *kvm, uint64_t pa, uint64_t len);

struct vfio_iommu_func {
    int (*handle_unmap)(struct kvm* kvm, uint64_t iova, uint64_t size);
	int (*handle_map)(struct kvm* kvm, uint64_t iova, uint64_t size);
	int (*handle_get_pfn)(struct kvm *kvm, uint64_t iova, unsigned long *pfn);
	/** Same as handle_get_pfn but uses mutex_trylock; returns -EBUSY if lock busy (hypercall-safe). */
	int (*handle_get_pfn_try)(struct kvm *kvm, uint64_t iova, unsigned long *pfn);
};

#endif /* _LINUX_COORDINATOR_H */
