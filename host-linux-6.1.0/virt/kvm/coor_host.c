#include <linux/coordinator.h>
#include <linux/frontswap.h>
#include <linux/swapops.h>
#include <linux/vfio.h>
#include <linux/mman.h>

int do_inflate_report_range(struct kvm *kvm, uint64_t pa, uint64_t len)
{
	uint64_t hva = 0;
	int ret;
#ifdef LAT_PROFILING
	uint64_t pf_time = 0;
#endif
	if (!len)
		len = PAGE_SIZE;

	if ((pa | len) & (PAGE_SIZE - 1))
		return -EINVAL;

	if (pa + len < pa)
		return -EINVAL;

	hva = gfn_to_hva(kvm, pa >> PAGE_SHIFT);

	if(kvm_is_error_hva(hva)) {
		pr_err("do_inflate_report_range: kvm_is_error_hva hva=%llx\n", (unsigned long long)hva);
		return -EFAULT;
	}

	if (kvm->vfio_iommu_func) {
#ifdef LAT_PROFILING
		pf_time = get_cycles_start();
#endif
		ret = ((struct vfio_iommu_func *)kvm->vfio_iommu_func)->handle_unmap(kvm, pa, len);
#ifdef LAT_PROFILING
		pf_time = get_cycles_end() - pf_time;
		if (len == PAGE_SIZE)
			accum_adc_time_stat(ADC_FILL_IOPT_LAT, pf_time);
#endif
		if (ret) {
			pr_err("do_inflate_report_range: handle_unmap failed ret=%d pa=%llx len=%llx\n",
			       ret, (unsigned long long)pa, (unsigned long long)len);
			return ret;
		}
	}

#ifdef LAT_PROFILING
	pf_time = get_cycles_start();
	ret = do_madvise(kvm->mm, hva, len, MADV_DONTNEED);
	// ret = balloon_madvise(kvm->mm, hva, len);
	pf_time = get_cycles_end() - pf_time;
	if (len == PAGE_SIZE)
		accum_adc_time_stat(ADC_FILL_HPT_LAT, pf_time);
#else
	ret = do_madvise(kvm->mm, hva, len, MADV_DONTNEED);
	// ret = balloon_madvise(kvm->mm, hva, len);
#endif
	if (ret) {
		pr_err("do_inflate_report_range: do_madvise failed ret=%d pa=%llx len=%llx hva=%llx\n",
		       ret, (unsigned long long)pa, (unsigned long long)len,
		       (unsigned long long)hva);
		return ret;
	}

	return 0;
}
/* free page reporting use */
static int do_inflate_report(struct kvm* kvm) {
	uint64_t inflate_end = kvm->inflate_end;
	uint64_t inflate_range = kvm->inflate_range;

	if (!inflate_end || !inflate_range)
		return -1;

	return do_inflate_report_range(kvm, inflate_end - inflate_range,
				       inflate_range);
}
int commit_inflate_report(struct kvm* kvm){
    int ret;
	ret = do_inflate_report(kvm);
	kvm->inflate_end = 0;
    kvm->inflate_range = 0;
    return ret;
}
int add_inflate_report(uint64_t pa, struct kvm* kvm, uint64_t len){
    int ret = 0;
    if (len == 0)
        len = PAGE_SIZE;
    if (kvm->inflate_end) {
        if (kvm->inflate_end == pa) {
            kvm->inflate_end += len;
            kvm->inflate_range += len;
            return 0;
        }
        else if ((kvm->inflate_end - kvm->inflate_range) == (pa + len)) {
            kvm->inflate_range += len;
            return 0;
        }
        else {
            ret = do_inflate_report(kvm);
            kvm->inflate_end = pa + len;
            kvm->inflate_range = len;
            return ret;
        }
    }
    else {
        kvm->inflate_end = pa + len;
        kvm->inflate_range = len;
        return 0;
    }
}

int do_deflate_report_range(struct kvm_vcpu *vcpu, uint64_t pa, uint64_t len)
{
	struct kvm *kvm;
	int ret = 0;
#ifdef LAT_PROFILING
	uint64_t pf_time = 0;
#endif
	uint64_t cur;
	unsigned long hva;

	if (!vcpu)
		return -EINVAL;
	kvm = vcpu->kvm;

	if (!len)
		len = PAGE_SIZE;

	if ((pa | len) & (PAGE_SIZE - 1))
		return -EINVAL;

	if (pa + len < pa)
		return -EINVAL;

	/*
	 * One guest page at a time: MADV_POPULATE_WRITE, then VFIO handle_map
	 * for that IOVA.
	 *
	 * Per-page IOPT (device DMA / IOMMU) mappings match the granularity at
	 * which we can adjust host memory and DMA visibility: fine-grained
	 * memory and IOPT updates stay flexible and aligned. 
	 * The VFIO side installs mappings in the same page-sized units; 
	 * see handle_map in vfio_iommu_type1.c (FINEGRAINED_DMA_SUPPORT).
	 */
	for (cur = pa; cur < pa + len; cur += PAGE_SIZE) {
		hva = gfn_to_hva(kvm, cur >> PAGE_SHIFT);
		if (kvm_is_error_hva(hva)) {
			pr_err("do_deflate_report_range: kvm_is_error_hva hva=%lx gfn=%llx\n",
			       hva, (unsigned long long)(cur >> PAGE_SHIFT));
			return -EFAULT;
		}

		ret = do_madvise(kvm->mm, hva, PAGE_SIZE, MADV_POPULATE_WRITE);
		if (ret) {
			pr_err("do_deflate_report_range: do_madvise failed ret=%d pa=%llx\n",
			       ret, (unsigned long long)cur);
			return ret;
		}

		if (kvm->vfio_iommu_func) {
			kvm_pfn_t pfn_before = gfn_to_pfn(kvm, cur >> PAGE_SHIFT);

			if (!is_error_noslot_pfn(pfn_before))
				kvm_release_pfn_clean(pfn_before);

#ifdef LAT_PROFILING
			pf_time = get_cycles_start();
#endif
			ret = ((struct vfio_iommu_func *)kvm->vfio_iommu_func)
				      ->handle_map(kvm, cur, PAGE_SIZE);
#ifdef LAT_PROFILING
			pf_time = get_cycles_end() - pf_time;
			accum_adc_time_stat(ADC_LEAK_IOPT_LAT, pf_time);
#endif
			if (ret) {
				pr_err("do_deflate_report_range: handle_map failed ret=%d pa=%llx\n",
				       ret, (unsigned long long)cur);
				return ret;
			}
		} 
		// else {
		// 	pr_err("Detect no vfio in this invocation!");
		// }
		schedule();
	}

	return 0;
}
