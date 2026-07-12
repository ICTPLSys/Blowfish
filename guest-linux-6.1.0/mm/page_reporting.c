// SPDX-License-Identifier: GPL-2.0
#include <linux/mm.h>
#include <linux/mmzone.h>
#include <linux/page_reporting.h>
#include <linux/gfp.h>
#include <linux/export.h>
#include <linux/module.h>
#include <linux/delay.h>
#include <linux/scatterlist.h>

#include "page_reporting.h"
#include "internal.h"
#include <linux/sysfs.h>
#include <linux/kobject.h>

// unsigned int page_reporting_order = HUGETLB_PAGE_ORDER < (MAX_ORDER - 1) ? HUGETLB_PAGE_ORDER : (MAX_ORDER - 1);
unsigned int page_reporting_order = 0;
module_param(page_reporting_order, uint, 0644);
MODULE_PARM_DESC(page_reporting_order, "Set page reporting order");

// unsigned int page_reporting_delay = 5 * HZ;
unsigned int page_reporting_delay = HZ / 100;
// unsigned int page_reporting_delay = HZ / 2000;
module_param(page_reporting_delay, uint, 0644);
MODULE_PARM_DESC(page_reporting_delay, "Set the page reporting delay");

unsigned int page_reporting_capacity = 32;
module_param(page_reporting_capacity, uint , 0644);
MODULE_PARM_DESC(page_reporting_capacity, "Set the page reporting capacity");

static unsigned long page_reporting_limit_target = 0;
// enable ondemand reporting or not, default to 0 (disabled)
static unsigned int page_reporting_limit_enable = 0;
static atomic64_t page_reporting_total_reported = ATOMIC64_INIT(0);
static struct kobject *onreclaim_kobj;

static ssize_t target_show(struct kobject *kobj, struct kobj_attribute *attr, char *buf)
{
	return sprintf(buf, "%lu\n", page_reporting_limit_target);
}
static ssize_t target_store(struct kobject *kobj, struct kobj_attribute *attr, const char *buf, size_t count)
{
	int err = kstrtoul(buf, 10, &page_reporting_limit_target);
	if (err) return err;
	return count;
}
static struct kobj_attribute target_attr = __ATTR(target, 0644, target_show, target_store);

static ssize_t enable_show(struct kobject *kobj, struct kobj_attribute *attr, char *buf)
{
	return sprintf(buf, "%u\n", page_reporting_limit_enable);
}
static ssize_t enable_store(struct kobject *kobj, struct kobj_attribute *attr, const char *buf, size_t count)
{
	int err = kstrtouint(buf, 10, &page_reporting_limit_enable);
	if (err) return err;
	return count;
}
static struct kobj_attribute enable_attr = __ATTR(enable, 0644, enable_show, enable_store);

static struct attribute *onreclaim_attrs[] = {
	&target_attr.attr,
	&enable_attr.attr,
	NULL,
};
static struct attribute_group onreclaim_attr_group = {
	.attrs = onreclaim_attrs,
};

#define REPORTING_CPU_USAGE
#ifdef REPORTING_CPU_USAGE
#include <linux/ktime.h>
#include <linux/kobject.h>
#include <linux/sysfs.h>
static ktime_t page_reporting_start_time;
static atomic64_t page_reporting_total_cpu_time;
static atomic_t page_reporting_run_count;
static bool page_reporting_first_run = true;
static struct kobject *reporting_kobj;
extern struct kobject *mm_kobj;
#endif

#define PAGE_REPORTING_DELAY	page_reporting_delay
static struct page_reporting_dev_info __rcu *pr_dev_info __read_mostly;

enum {
	PAGE_REPORTING_IDLE = 0,
	PAGE_REPORTING_REQUESTED,
	PAGE_REPORTING_ACTIVE
};

/* request page reporting */
static void
__page_reporting_request(struct page_reporting_dev_info *prdev)
{
	unsigned int state;

	/* Check to see if we are in desired state */
	state = atomic_read(&prdev->state);

	if (state != PAGE_REPORTING_IDLE)
        return;
    schedule_delayed_work(&prdev->work, PAGE_REPORTING_DELAY);
	// if (state == PAGE_REPORTING_REQUESTED)
	// 	return;

	// /*
	//  * If reporting is already active there is nothing we need to do.
	//  * Test against 0 as that represents PAGE_REPORTING_IDLE.
	//  */
	// state = atomic_xchg(&prdev->state, PAGE_REPORTING_REQUESTED);
	// if (state != PAGE_REPORTING_IDLE)
	// 	return;

	// /*
	//  * Delay the start of work to allow a sizable queue to build. For
	//  * now we are limiting this to running no more than once every
	//  * couple of seconds.
	//  */
	// schedule_delayed_work(&prdev->work, PAGE_REPORTING_DELAY);
}

/* notify prdev of free page reporting request */
void __page_reporting_notify(void)
{
	return;

	// struct page_reporting_dev_info *prdev;

	// /*
	//  * We use RCU to protect the pr_dev_info pointer. In almost all
	//  * cases this should be present, however in the unlikely case of
	//  * a shutdown this will be NULL and we should exit.
	//  */
	// rcu_read_lock();
	// prdev = rcu_dereference(pr_dev_info);
	// if (likely(prdev))
	// 	__page_reporting_request(prdev);

	// rcu_read_unlock();
}

static void
page_reporting_drain(struct page_reporting_dev_info *prdev,
		     struct scatterlist *sgl, unsigned int nents, bool reported)
{
	struct scatterlist *sg = sgl;

	/*
	 * Drain the now reported pages back into their respective
	 * free lists/areas. We assume at least one page is populated.
	 */
	do {
		struct page *page = sg_page(sg);
		int mt = get_pageblock_migratetype(page);
		unsigned int order = get_order(sg->length);

		__putback_isolated_page(page, order, mt);

		/* If the pages were not reported due to error skip flagging */
		if (!reported)
			continue;

		/*
		 * If page was not comingled with another page we can
		 * consider the result to be "reported" since the page
		 * hasn't been modified, otherwise we will need to
		 * report on the new larger page when we make our way
		 * up to that higher order.
		 */
		if (PageBuddy(page) && buddy_order(page) == order)
			__SetPageReported(page);
	} while ((sg = sg_next(sg)));

	/* reinitialize scatterlist now that it is empty */
	sg_init_table(sgl, nents);
}

/*
 * The page reporting cycle consists of 4 stages, fill, report, drain, and
 * idle. We will cycle through the first 3 stages until we cannot obtain a
 * full scatterlist of pages, in that case we will switch to idle.
 */
static int
page_reporting_cycle(struct page_reporting_dev_info *prdev, struct zone *zone,
		     unsigned int order, unsigned int mt,
		     struct scatterlist *sgl, unsigned int *offset)
{
	struct free_area *area = &zone->free_area[order];
	struct list_head *list = &area->free_list[mt];
	unsigned int page_len = PAGE_SIZE << order;
	struct page *page, *next;
	long budget;
	int err = 0;

	/*
	 * Perform early check, if free area is empty there is
	 * nothing to process so we can skip this free_list.
	 */
	if (list_empty(list))
		return err;

	spin_lock_irq(&zone->lock);

	/*
	 * Limit how many calls we will be making to the page reporting
	 * device for this list. By doing this we avoid processing any
	 * given list for too long.
	 *
	 * The current value used allows us enough calls to process over a
	 * sixteenth of the current list plus one additional call to handle
	 * any pages that may have already been present from the previous
	 * list processed. This should result in us reporting all pages on
	 * an idle system in about 30 seconds.
	 *
	 * The division here should be cheap since PAGE_REPORTING_CAPACITY
	 * should always be a power of 2.
	 */
	budget = DIV_ROUND_UP(area->nr_free, PAGE_REPORTING_CAPACITY * 16);

	/* loop through free list adding unreported pages to sg list */
	list_for_each_entry_safe(page, next, list, lru) {
		/* We are going to skip over the reported pages. */
		if (PageReported(page))
			continue;

		if (page_reporting_limit_enable && 
			atomic64_read(&page_reporting_total_reported) >= page_reporting_limit_target) {
			atomic_set(&prdev->state, PAGE_REPORTING_IDLE);
			err = 0;
			break;
		}

		/*
		 * If we fully consumed our budget then update our
		 * state to indicate that we are requesting additional
		 * processing and exit this list.
		 */
		if (budget < 0) {
			atomic_set(&prdev->state, PAGE_REPORTING_REQUESTED);
			next = page;
			break;
		}

		/* Attempt to pull page from list and place in scatterlist */
		if (*offset) {
			if (!__isolate_free_page(page, order)) {
				next = page;
				break;
			}

			/* Add page to scatter list */
			--(*offset);
			sg_set_page(&sgl[*offset], page, page_len, 0);

			continue;
		}

		/*
		 * Make the first non-reported page in the free list
		 * the new head of the free list before we release the
		 * zone lock.
		 */
		if (!list_is_first(&page->lru, list))
			list_rotate_to_front(&page->lru, list);

		/* release lock before waiting on report processing */
		spin_unlock_irq(&zone->lock);

		/* begin processing pages in local list */
		err = prdev->report(prdev, sgl, PAGE_REPORTING_CAPACITY);

		/* reset offset since the full list was reported */
		*offset = PAGE_REPORTING_CAPACITY;

		if (!err)
			atomic64_add(PAGE_REPORTING_CAPACITY * (1 << order), &page_reporting_total_reported);

		/* update budget to reflect call to report function */
		budget--;

		/* reacquire zone lock and resume processing */
		spin_lock_irq(&zone->lock);

		/* flush reported pages from the sg list */
		page_reporting_drain(prdev, sgl, PAGE_REPORTING_CAPACITY, !err);

		/*
		 * Reset next to first entry, the old next isn't valid
		 * since we dropped the lock to report the pages
		 */
		next = list_first_entry(list, struct page, lru);

		/* exit on error */
		if (err)
			break;
	}

	/* Rotate any leftover pages to the head of the freelist */
	if (!list_entry_is_head(next, list, lru) && !list_is_first(&next->lru, list))
		list_rotate_to_front(&next->lru, list);

	spin_unlock_irq(&zone->lock);

	return err;
}

static int
page_reporting_process_zone(struct page_reporting_dev_info *prdev,
			    struct scatterlist *sgl, struct zone *zone)
{
	unsigned int order, mt, leftover, offset = PAGE_REPORTING_CAPACITY;
	unsigned long watermark;
	int err = 0;

	/* Generate minimum watermark to be able to guarantee progress */
	watermark = low_wmark_pages(zone) +
		    (PAGE_REPORTING_CAPACITY << page_reporting_order);

	/*
	 * Cancel request if insufficient free memory or if we failed
	 * to allocate page reporting statistics for the zone.
	 */
	if (!zone_watermark_ok(zone, 0, watermark, 0, ALLOC_CMA))
		return err;

	/* Process each free list starting from lowest order/mt */
	// if (page_reporting_limit_enable) {
	// 	for (order = MAX_ORDER; order-- > page_reporting_order; ) {
	// 		for (mt = 0; mt < MIGRATE_TYPES; mt++) {
	// 			/* We do not pull pages from the isolate free list */
	// 			if (is_migrate_isolate(mt))
	// 				continue;
	// 			// printk(KERN_INFO "page_reporting_cycle: order %d, mt %d\n", order, mt);
	// 			err = page_reporting_cycle(prdev, zone, order, mt,
	// 						   sgl, &offset);
	// 			if (err)
	// 				return err;
	// 		}
	// 	}
	// } else {
	for (order = page_reporting_order; order < MAX_ORDER; order++) {
		for (mt = 0; mt < MIGRATE_TYPES; mt++) {
			/* We do not pull pages from the isolate free list */
			if (is_migrate_isolate(mt))
				continue;

			err = page_reporting_cycle(prdev, zone, order, mt,
							sgl, &offset);
			if (err)
				return err;
		}
	}
	// }

	/* report the leftover pages before going idle */
	leftover = PAGE_REPORTING_CAPACITY - offset;
	if (leftover) {
		unsigned int i;
		unsigned long leftover_pages = 0;

		for (i = offset; i < PAGE_REPORTING_CAPACITY; i++)
			leftover_pages += sgl[i].length >> PAGE_SHIFT;

		sgl = &sgl[offset];
		err = prdev->report(prdev, sgl, leftover);
		if (!err)
			atomic64_add(leftover_pages, &page_reporting_total_reported);
		/* flush any remaining pages out from the last report */
		spin_lock_irq(&zone->lock);
		page_reporting_drain(prdev, sgl, leftover, !err);
		spin_unlock_irq(&zone->lock);
	}

	return err;
}

#ifdef REPORTING_CPU_USAGE
static ssize_t cpu_usage_show(struct kobject *kobj, struct kobj_attribute *attr, char *buf)
{
    ktime_t current_time = ktime_get();
    u64 total_cpu_time_ns = atomic64_read(&page_reporting_total_cpu_time);
    u64 elapsed_time_ns;
    u64 cpu_usage = 0;
    int run_count = atomic_read(&page_reporting_run_count);
    
    if (page_reporting_first_run)
        return scnprintf(buf, PAGE_SIZE, "Page reporting not started yet\n");
    
    elapsed_time_ns = ktime_to_ns(ktime_sub(current_time, page_reporting_start_time));
    
    if (elapsed_time_ns > 0)
        cpu_usage = (total_cpu_time_ns * 10000) / elapsed_time_ns;
    
    return scnprintf(buf, PAGE_SIZE,
            "CPU usage: %llu.%02llu%%\n"
            "Runs: %d\n"
            "Total CPU time: %llu ns\n"
            "Elapsed time: %llu ns\n"
            "Average run time: %llu ns\n",
            cpu_usage / 100, cpu_usage % 100,
            run_count, 
            total_cpu_time_ns, 
            elapsed_time_ns,
            run_count > 0 ? total_cpu_time_ns / run_count : 0);
}
static struct kobj_attribute cpu_usage_attr = 
    __ATTR(cpu_usage, 0444, cpu_usage_show, NULL);
#endif
static void page_reporting_process(struct work_struct *work)
{
	struct delayed_work *d_work = to_delayed_work(work);
	struct page_reporting_dev_info *prdev =
		container_of(d_work, struct page_reporting_dev_info, work);
	int err = 0, state = PAGE_REPORTING_ACTIVE;
	struct scatterlist *sgl;
	struct zone *zone;
#ifdef REPORTING_CPU_USAGE
	ktime_t start_time, end_time, cpu_time;
    int run_count;
	start_time = ktime_get();
	if (page_reporting_first_run) {
        page_reporting_start_time = start_time;
        page_reporting_first_run = false;
        atomic64_set(&page_reporting_total_cpu_time, 0);
        atomic_set(&page_reporting_run_count, 0);
    }
#endif
	/*
	 * Change the state to "Active" so that we can track if there is
	 * anyone requests page reporting after we complete our pass. If
	 * the state is not altered by the end of the pass we will switch
	 * to idle and quit scheduling reporting runs.
	 */
	atomic_set(&prdev->state, state);

	/* allocate scatterlist to store pages being reported on */
	sgl = kmalloc_array(PAGE_REPORTING_CAPACITY, sizeof(*sgl), GFP_KERNEL);
	if (!sgl)
		goto err_out;

	sg_init_table(sgl, PAGE_REPORTING_CAPACITY);

	for_each_zone(zone) {
		if (page_reporting_limit_enable && 
			atomic64_read(&page_reporting_total_reported) >= page_reporting_limit_target)
			break;

		err = page_reporting_process_zone(prdev, sgl, zone);
		if (err)
			break;
	}

	kfree(sgl);
err_out:
	/*
	 * If the state has reverted back to requested then there may be
	 * additional pages to be processed. We will defer for 2s to allow
	 * more pages to accumulate.
	 */
	// state = atomic_cmpxchg(&prdev->state, state, PAGE_REPORTING_IDLE);
	// if (state == PAGE_REPORTING_REQUESTED)
	// 	schedule_delayed_work(&prdev->work, PAGE_REPORTING_DELAY);
#ifdef REPORTING_CPU_USAGE
	end_time = ktime_get();
    cpu_time = ktime_sub(end_time, start_time);
    atomic64_add(ktime_to_ns(cpu_time), &page_reporting_total_cpu_time);
    run_count = atomic_inc_return(&page_reporting_run_count);
#endif
	// Always set the state to idle, so that we can re-queue
	atomic_set(&prdev->state, PAGE_REPORTING_IDLE);

	if (page_reporting_limit_enable && 
		atomic64_read(&page_reporting_total_reported) < page_reporting_limit_target)
		schedule_delayed_work(&prdev->work, 0);
	else
		schedule_delayed_work(&prdev->work, PAGE_REPORTING_DELAY);
}

static DEFINE_MUTEX(page_reporting_mutex);
DEFINE_STATIC_KEY_FALSE(page_reporting_enabled);

int page_reporting_register(struct page_reporting_dev_info *prdev)
{
	int err = 0;

	mutex_lock(&page_reporting_mutex);

	/* nothing to do if already in use */
	if (rcu_access_pointer(pr_dev_info)) {
		err = -EBUSY;
		goto err_out;
	}
#ifdef REPORTING_CPU_USAGE
	if (!reporting_kobj) {
        if (!mm_kobj) {
            mm_kobj = kobject_create_and_add("mm", kernel_kobj);
            if (!mm_kobj) {
                err = -ENOMEM;
                goto err_out;
            }
        }
        reporting_kobj = kobject_create_and_add("reporting", mm_kobj);
        if (!reporting_kobj) {
            err = -ENOMEM;
            goto err_out;
        }
        err = sysfs_create_file(reporting_kobj, &cpu_usage_attr.attr);
        if (err) {
            kobject_put(reporting_kobj);
            reporting_kobj = NULL;
            goto err_out;
        }
        pr_info("Created page reporting sysfs interface: /sys/kernel/mm/reporting/cpu_usage\n");
	}
#endif
	/*
	 * Update the page reporting order if it's specified by driver.
	 */
	page_reporting_order = prdev->order ?: page_reporting_order;

	/* initialize state and work structures */
	atomic_set(&prdev->state, PAGE_REPORTING_IDLE);
	INIT_DELAYED_WORK(&prdev->work, &page_reporting_process);

	/* Begin initial flush of zones */
	__page_reporting_request(prdev);

	/* Assign device to allow notifications */
	rcu_assign_pointer(pr_dev_info, prdev);

	/* enable page reporting notification */
	if (!static_key_enabled(&page_reporting_enabled)) {
		static_branch_enable(&page_reporting_enabled);
		pr_info("Free page reporting enabled\n");
	}

	if (!onreclaim_kobj) {
		onreclaim_kobj = kobject_create_and_add("onreclaim", kernel_kobj);
		if (onreclaim_kobj) {
			if (sysfs_create_group(onreclaim_kobj, &onreclaim_attr_group)) {
				kobject_put(onreclaim_kobj);
				onreclaim_kobj = NULL;
			}
		}
	}

err_out:
	mutex_unlock(&page_reporting_mutex);

	return err;
}
EXPORT_SYMBOL_GPL(page_reporting_register);

void page_reporting_unregister(struct page_reporting_dev_info *prdev)
{
	mutex_lock(&page_reporting_mutex);

	if (onreclaim_kobj) {
		sysfs_remove_group(onreclaim_kobj, &onreclaim_attr_group);
		kobject_put(onreclaim_kobj);
		onreclaim_kobj = NULL;
	}

	if (rcu_access_pointer(pr_dev_info) == prdev) {
		/* Disable page reporting notification */
		RCU_INIT_POINTER(pr_dev_info, NULL);
		synchronize_rcu();

		/* Flush any existing work, and lock it out */
		cancel_delayed_work_sync(&prdev->work);
	}
#ifdef REPORTING_CPU_USAGE
	if (reporting_kobj) {
        sysfs_remove_file(reporting_kobj, &cpu_usage_attr.attr);
        kobject_put(reporting_kobj);
        reporting_kobj = NULL;
    }
#endif
	mutex_unlock(&page_reporting_mutex);
}
EXPORT_SYMBOL_GPL(page_reporting_unregister);
