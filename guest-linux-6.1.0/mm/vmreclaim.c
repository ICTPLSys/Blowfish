#include <linux/workqueue.h>
#include <linux/init.h>
#include <linux/vmscan.h>
#include <linux/sysfs.h>
#include <linux/kobject.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/mmzone.h>
#include <linux/completion.h>
#include <linux/atomic.h>
#include <linux/spinlock.h>
#include <linux/build_bug.h>
#include <linux/kvm_para.h>
#include <linux/smp.h>

#include <linux/dma-profiling.h>
#include <linux/moduleparam.h>
#include <linux/blowfish-reclaim.h>

#define RECLAIM_CPU_USAGE
#define MAX_RECLAIM_THREADS 32

#ifdef RECLAIM_CPU_USAGE
#include <linux/ktime.h> 
static ktime_t reclaim_start_time;
static atomic64_t reclaim_total_cpu_time;
static atomic_t reclaim_run_count;
static bool reclaim_first_run = true; 
#endif

static atomic_t reclaim_pages_target = ATOMIC_INIT(0);
static atomic_t reclaim_in_progress = ATOMIC_INIT(0);
static atomic_t reclaim_pages_remaining = ATOMIC_INIT(0);
static atomic_t active_workers = ATOMIC_INIT(0);

static unsigned int reclaim_thread_count = 1;
static struct workqueue_struct *reclaim_wq;
static struct work_struct reclaim_works[MAX_RECLAIM_THREADS];
#ifdef RECLAIM_CPU_USAGE
static DEFINE_SPINLOCK(reclaim_lock);
#endif

#ifdef CONFIG_BLOWFISH

bool blowfish_use_async_reclaim __read_mostly = false;
core_param(blowfish_async_reclaim, blowfish_use_async_reclaim, bool, 0444);

u64 *restore_buffer;
size_t restore_buffer_size;
u64 *reclaim_gfn_buffer;
u32 *reclaim_len_buffer;
size_t reclaim_buffer_size;

struct blowfish_async_ring_hdr {
	atomic64_t producer;
	atomic64_t consumer;
	u8 pad[BLOWFISH_ASYNC_HEADER_BYTES - 2 * sizeof(atomic64_t)];
};

static u64 *blowfish_async_reclaim_vbase;
static unsigned int blowfish_async_reclaim_n_slots;
static DEFINE_SPINLOCK(blowfish_async_enqueue_lock);
bool blowfish_async_reclaim_registered;

int blowfish_async_reclaim_try_enqueue(u64 gfn, u32 len)
{
	struct blowfish_async_ring_hdr *hdr;
	u64 *entries;
	u64 *slot;
	unsigned long flags;
	unsigned int idx;
	int ret = 0;

	if (!blowfish_async_reclaim_registered || !blowfish_async_reclaim_vbase)
		return -ENODEV;

	hdr = (struct blowfish_async_ring_hdr *)blowfish_async_reclaim_vbase;
	entries = (u64 *)((u8 *)hdr + BLOWFISH_ASYNC_HEADER_BYTES);

	spin_lock_irqsave(&blowfish_async_enqueue_lock, flags);
	for (;;) {
		u64 p = atomic64_read(&hdr->producer);
		u64 c = atomic64_read(&hdr->consumer);
		u64 pending = p - c;

		if (pending >= (u64)blowfish_async_reclaim_n_slots) {
			ret = -EBUSY;
			break;
		}

		idx = (unsigned int)(p % (u64)blowfish_async_reclaim_n_slots);
		slot = &entries[(u64)idx * 2];

		if (READ_ONCE(slot[0])) {
			spin_unlock_irqrestore(&blowfish_async_enqueue_lock, flags);
			cond_resched();
			spin_lock_irqsave(&blowfish_async_enqueue_lock, flags);
			continue;
		}

		WRITE_ONCE(slot[1], (u64)len);
		// smp_wmb();
		WRITE_ONCE(slot[0], gfn);
		atomic64_inc(&hdr->producer);
		ret = 0;
		break;
	}
	spin_unlock_irqrestore(&blowfish_async_enqueue_lock, flags);

	return ret;
}

static void blowfish_async_reclaim_teardown(void)
{
	blowfish_async_reclaim_registered = false;
	smp_mb();
	if (blowfish_async_reclaim_vbase) {
		free_pages((unsigned long)blowfish_async_reclaim_vbase,
			   BLOWFISH_ASYNC_PAGE_ORDER);
		blowfish_async_reclaim_vbase = NULL;
	}
	blowfish_async_reclaim_n_slots = 0;
}

static int blowfish_async_reclaim_ring_init(void)
{
	long hc_ret;
	u64 *vbase;

	BUILD_BUG_ON((1UL << BLOWFISH_ASYNC_PAGE_ORDER) != BLOWFISH_ASYNC_TOTAL_PAGES);

	vbase = (u64 *)__get_free_pages(GFP_KERNEL | __GFP_ZERO, BLOWFISH_ASYNC_PAGE_ORDER);
	if (!vbase)
		return -ENOMEM;

	blowfish_async_reclaim_n_slots = BLOWFISH_ASYNC_N_SLOTS();
	blowfish_async_reclaim_vbase = vbase;

	hc_ret = kvm_hypercall1(KVM_HC_BLOWFISH_ASYNC_RECLAIM_REGISTER,
				(unsigned long)__pa(vbase));
	if (hc_ret < 0) {
		blowfish_async_reclaim_vbase = NULL;
		blowfish_async_reclaim_n_slots = 0;
		free_pages((unsigned long)vbase, BLOWFISH_ASYNC_PAGE_ORDER);
		return (int)hc_ret;
	}

	blowfish_async_reclaim_registered = true;
	smp_wmb();
	pr_info("Blowfish: async reclaim ring registered (%u slots, %lu pages)\n",
		blowfish_async_reclaim_n_slots, BLOWFISH_ASYNC_TOTAL_PAGES);
	return 0;
}

static size_t restore_buffer_idx;
static DEFINE_SPINLOCK(restore_buffer_lock);

int blowfish_restore_buffer_pop(u64 *pfn)
{
	unsigned long flags;

	if (!restore_buffer || !restore_buffer_size || !pfn)
		return 0;

	if (!spin_trylock_irqsave(&restore_buffer_lock, flags))
		return 0;

	if (restore_buffer[restore_buffer_idx] == 0) {
		spin_unlock_irqrestore(&restore_buffer_lock, flags);
		return 0;
	}

	*pfn = restore_buffer[restore_buffer_idx];
	restore_buffer[restore_buffer_idx] = 0;
	restore_buffer_idx = (restore_buffer_idx + 1) % restore_buffer_size;

	spin_unlock_irqrestore(&restore_buffer_lock, flags);
	return 1;
}

static int __init blowfish_buffers_init(void)
{
	unsigned long restore_bytes;
	unsigned long reclaim_bytes;
	int ret;

	restore_buffer = (u64 *)__get_free_pages(GFP_KERNEL | __GFP_ZERO,
						 BLOWFISH_RESTORE_BUFFER_ORDER);
	if (!restore_buffer)
		return -ENOMEM;

	restore_bytes = PAGE_SIZE << BLOWFISH_RESTORE_BUFFER_ORDER;
	restore_buffer_size = restore_bytes / sizeof(u64);
	restore_buffer_idx = 0;

	ret = kvm_hypercall3(KVM_HC_BLOWFISH_REGISTER_BUFFER,
			     __pa(restore_buffer),
			     restore_buffer_size,
			     BLOWFISH_BUFFER_TYPE_RESTORE);
	if (ret < 0)
		goto err_restore;

	if (blowfish_use_async_reclaim) {
		pr_info("Blowfish: restore buffer registered (async reclaim mode)\n");
		ret = blowfish_async_reclaim_ring_init();
		if (ret) {
			pr_err("Blowfish: async reclaim ring init failed (%d)\n", ret);
			goto err_restore;
		}
		return 0;
	}

	reclaim_gfn_buffer = (u64 *)__get_free_pages(GFP_KERNEL | __GFP_ZERO,
						      BLOWFISH_RECLAIM_BUFFER_ORDER);
	if (!reclaim_gfn_buffer) {
		ret = -ENOMEM;
		goto err_restore;
	}
	reclaim_len_buffer = (u32 *)__get_free_pages(GFP_KERNEL | __GFP_ZERO,
						      BLOWFISH_RECLAIM_BUFFER_ORDER);
	if (!reclaim_len_buffer) {
		free_pages((unsigned long)reclaim_gfn_buffer, BLOWFISH_RECLAIM_BUFFER_ORDER);
		reclaim_gfn_buffer = NULL;
		ret = -ENOMEM;
		goto err_restore;
	}
	reclaim_bytes = PAGE_SIZE << BLOWFISH_RECLAIM_BUFFER_ORDER;
	reclaim_buffer_size = reclaim_bytes / sizeof(*reclaim_gfn_buffer);

	ret = kvm_hypercall4(KVM_HC_BLOWFISH_REGISTER_BUFFER,
			     __pa(reclaim_gfn_buffer),
			     __pa(reclaim_len_buffer),
			     reclaim_buffer_size,
			     BLOWFISH_BUFFER_TYPE_RECLAIM);
	if (ret < 0)
		goto err_sync_reclaim;

	pr_info("Blowfish: restore/reclaim buffers registered (sync reclaim)\n");

	return 0;

err_sync_reclaim:
	free_pages((unsigned long)reclaim_len_buffer, BLOWFISH_RECLAIM_BUFFER_ORDER);
	reclaim_len_buffer = NULL;
	free_pages((unsigned long)reclaim_gfn_buffer, BLOWFISH_RECLAIM_BUFFER_ORDER);
	reclaim_gfn_buffer = NULL;
	reclaim_buffer_size = 0;
err_restore:
	free_pages((unsigned long)restore_buffer, BLOWFISH_RESTORE_BUFFER_ORDER);
	restore_buffer = NULL;
	restore_buffer_size = 0;
	return ret;
}

static void blowfish_buffers_free(void)
{
	blowfish_async_reclaim_teardown();

	if (reclaim_len_buffer) {
		free_pages((unsigned long)reclaim_len_buffer, BLOWFISH_RECLAIM_BUFFER_ORDER);
		reclaim_len_buffer = NULL;
	}
	if (reclaim_gfn_buffer) {
		free_pages((unsigned long)reclaim_gfn_buffer, BLOWFISH_RECLAIM_BUFFER_ORDER);
		reclaim_gfn_buffer = NULL;
	}
	reclaim_buffer_size = 0;

	if (restore_buffer) {
		free_pages((unsigned long)restore_buffer, BLOWFISH_RESTORE_BUFFER_ORDER);
		restore_buffer = NULL;
	}
	restore_buffer_size = 0;
}
#endif

static void reclaim_pages_worker(struct work_struct *work)
{
    unsigned long reclaimed_pages = 0;
    unsigned long batch_size, remaining;
    int node;
    
#ifdef RECLAIM_CPU_USAGE
    ktime_t func_start_time, func_end_time, cpu_time;
    func_start_time = ktime_get();
    
    if (reclaim_first_run) {
        spin_lock(&reclaim_lock);
        if (reclaim_first_run) {
            reclaim_start_time = func_start_time;
            reclaim_first_run = false;
            atomic64_set(&reclaim_total_cpu_time, 0);
            atomic_set(&reclaim_run_count, 0);
        }
        spin_unlock(&reclaim_lock);
    }
#endif

    while ((remaining = atomic_read(&reclaim_pages_remaining)) > 0) {
        batch_size = max(remaining / reclaim_thread_count / 2, 512UL);
        batch_size = min(batch_size, remaining);
        
        if (batch_size == 0)
            break;
            
        if (atomic_sub_return(batch_size, &reclaim_pages_remaining) < 0) {
            atomic_add(batch_size, &reclaim_pages_remaining);
            break;
        }
        
        for_each_online_node(node) {
            unsigned long node_batch = batch_size;
            //  / num_online_nodes();
            // if (node == numa_node_id() % num_online_nodes())
            //     node_batch += batch_size % num_online_nodes();
                
            if (node_batch > 0) {
                unsigned long node_reclaimed = shrink_pagecache_for_reclaim(node, node_batch);
                reclaimed_pages += node_reclaimed;
            }
        }
        
        cond_resched();
        // schedule();
        
        if (atomic_read(&reclaim_pages_remaining) <= 0)
            break;
    }

#ifdef RECLAIM_CPU_USAGE
    func_end_time = ktime_get();
    cpu_time = ktime_sub(func_end_time, func_start_time);
    atomic64_add(ktime_to_ns(cpu_time), &reclaim_total_cpu_time);
    atomic_inc(&reclaim_run_count);
#endif

    if (atomic_dec_and_test(&active_workers)) {
        atomic_set(&reclaim_pages_target, 0);
        atomic_set(&reclaim_pages_remaining, 0);
        atomic_set(&reclaim_in_progress, 0);
    }
}

static void start_reclaim_work(unsigned long target_pages)
{
    int i;

    atomic_set(&reclaim_pages_remaining, target_pages);
    atomic_set(&active_workers, reclaim_thread_count);
    
    for (i = 0; i < reclaim_thread_count; i++) {
        INIT_WORK(&reclaim_works[i], reclaim_pages_worker);
        queue_work(reclaim_wq, &reclaim_works[i]);
    }
}

#ifdef RECLAIM_CPU_USAGE
static ssize_t cpu_usage_show(struct kobject *kobj, struct kobj_attribute *attr,
                             char *buf)
{
    ktime_t current_time = ktime_get();
    u64 total_cpu_time_ns = atomic64_read(&reclaim_total_cpu_time);
    u64 elapsed_time_ns;
    u64 cpu_usage = 0;
    int run_count = atomic_read(&reclaim_run_count);
    
    if (reclaim_first_run)
        return sprintf(buf, "vmreclaim not started yet\n");
    
    elapsed_time_ns = ktime_to_ns(ktime_sub(current_time, reclaim_start_time));
    
    if (elapsed_time_ns > 0)
        cpu_usage = (total_cpu_time_ns * 10000) / elapsed_time_ns;
    
    return sprintf(buf, "CPU usage: %llu.%02llu%%\n"
                       "Runs: %d\n"
                       "Total CPU time: %llu ns\n"
                       "Elapsed time: %llu ns\n"
                       "Average run time: %llu ns\n"
                       "Active threads: %u\n",
                   cpu_usage / 100, cpu_usage % 100,
                   run_count, 
                   total_cpu_time_ns, 
                   elapsed_time_ns,
                   run_count > 0 ? total_cpu_time_ns / run_count : 0,
                   reclaim_thread_count);
}
#endif

static ssize_t pagenums_show(struct kobject *kobj, struct kobj_attribute *attr,
                           char *buf)
{
    return sprintf(buf, "%d\n", atomic_read(&reclaim_pages_target));
}

static ssize_t pagenums_store(struct kobject *kobj, struct kobj_attribute *attr,
                            const char *buf, size_t count)
{
    unsigned long pages;
    int ret;

    ret = kstrtoul(buf, 10, &pages);
    if (ret)
        return ret;

    atomic_set(&reclaim_pages_target, pages);
    atomic_set(&reclaim_pages_remaining, pages);

    if (atomic_cmpxchg(&reclaim_in_progress, 0, 1) == 0) {
        start_reclaim_work(pages);
    } else {
        printk(KERN_INFO "vmreclaim: updated target to %lu pages, reclaim already in progress\n", 
               pages);
    }

    return count;
}

static ssize_t thread_count_show(struct kobject *kobj, struct kobj_attribute *attr,
                                char *buf)
{
    return sprintf(buf, "%u\n", reclaim_thread_count);
}

static ssize_t thread_count_store(struct kobject *kobj, struct kobj_attribute *attr,
                                 const char *buf, size_t count)
{
    unsigned int threads;
    int ret;

    ret = kstrtouint(buf, 10, &threads);
    if (ret)
        return ret;

    if (threads == 0 || threads > MAX_RECLAIM_THREADS) {
        printk("vmreclaim: thread count must be between 1 and %d\n", 
               MAX_RECLAIM_THREADS);
        return -EINVAL;
    }

    if (atomic_read(&reclaim_in_progress)) {
        printk("vmreclaim: cannot change thread count while reclaim is in progress\n");
        return -EBUSY;
    }

    reclaim_thread_count = threads;
    printk("vmreclaim: thread count set to %u\n", threads);

    return count;
}

static ssize_t status_show(struct kobject *kobj, struct kobj_attribute *attr,
                          char *buf)
{
    return sprintf(buf, "Target pages: %d\n"
                       "Remaining pages: %d\n"
                       "In progress: %s\n"
                       "Active workers: %d\n"
                       "Thread count: %u\n",
                   atomic_read(&reclaim_pages_target),
                   atomic_read(&reclaim_pages_remaining),
                   atomic_read(&reclaim_in_progress) ? "yes" : "no",
                   atomic_read(&active_workers),
                   reclaim_thread_count);
}

static struct kobj_attribute pagenums_attribute = 
    __ATTR(pagenums, 0644, pagenums_show, pagenums_store);
static struct kobj_attribute thread_count_attribute = 
    __ATTR(thread_count, 0644, thread_count_show, thread_count_store);
static struct kobj_attribute status_attribute = 
    __ATTR(status, 0444, status_show, NULL);

#ifdef RECLAIM_CPU_USAGE
static struct kobj_attribute cpu_usage_attribute = 
    __ATTR(cpu_usage, 0444, cpu_usage_show, NULL);
#endif

static struct kobject *reclaim_kobj;

static int __init vmreclaim_init(void)
{
    int ret;

#ifdef CONFIG_BLOWFISH
    ret = blowfish_buffers_init();
    if (ret) {
        printk(KERN_ERR "vmreclaim: failed to init blowfish buffers: %d\n", ret);
        return ret;
    }
#endif

    reclaim_wq = alloc_workqueue("vmreclaim", WQ_MEM_RECLAIM | WQ_UNBOUND, 0);
    if (!reclaim_wq) {
        printk(KERN_ERR "vmreclaim: failed to create workqueue\n");
#ifdef CONFIG_BLOWFISH
        blowfish_buffers_free();
#endif
        return -ENOMEM;
    }

#ifdef RECLAIM_CPU_USAGE
    reclaim_first_run = true;
    atomic64_set(&reclaim_total_cpu_time, 0);
    atomic_set(&reclaim_run_count, 0);
#endif

    reclaim_kobj = kobject_create_and_add("reclaim", kernel_kobj);
    if (!reclaim_kobj) {
        destroy_workqueue(reclaim_wq);
        return -ENOMEM;
    }

    ret = sysfs_create_file(reclaim_kobj, &pagenums_attribute.attr);
    if (ret)
        goto error;

    ret = sysfs_create_file(reclaim_kobj, &thread_count_attribute.attr);
    if (ret)
        goto error;

    ret = sysfs_create_file(reclaim_kobj, &status_attribute.attr);
    if (ret)
        goto error;

#ifdef RECLAIM_CPU_USAGE
    ret = sysfs_create_file(reclaim_kobj, &cpu_usage_attribute.attr);
    if (ret)
        goto error;
#endif

    printk(KERN_INFO "vmreclaim: initialized with interfaces:\n");
    printk(KERN_INFO "  /sys/kernel/reclaim/pagenums - set target pages\n");
    printk(KERN_INFO "  /sys/kernel/reclaim/thread_count - set worker threads (1-%d)\n", 
           MAX_RECLAIM_THREADS);
    printk(KERN_INFO "  /sys/kernel/reclaim/status - view current status\n");
#ifdef RECLAIM_CPU_USAGE
    printk(KERN_INFO "  /sys/kernel/reclaim/cpu_usage - view CPU usage\n");
#endif

    return 0;

error:
    kobject_put(reclaim_kobj);
    destroy_workqueue(reclaim_wq);
#ifdef CONFIG_BLOWFISH
    blowfish_buffers_free();
#endif
    return ret;
}

fs_initcall(vmreclaim_init);