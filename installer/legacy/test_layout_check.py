"""Synthetic metadata fixtures: no block devices, fsck, mounts or large image writes."""
import copy
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
import uuid
import zlib
import layout_check as c

class Image:
    def __init__(self,total):
        self.size=total*512; self.regions=[]; self.reads=[]
    def put(self,offset,data): self.regions.append((offset,bytes(data)))
    def read(self,offset,length):
        if offset<0 or length<0 or length>65536 or offset+length>self.size:
            raise c.Rejected('fixture_bounds')
        self.reads.append((offset,length))
        out=bytearray(length)
        for start,data in self.regions:
            a=max(offset,start); b=min(offset+length,start+len(data))
            if a<b: out[a-offset:b-offset]=data[a-start:b-start]
        return bytes(out)
    def digest(self):
        return hashlib.sha256(repr(self.regions).encode()).hexdigest()

def gpt(total,triples,*,rows_edit=None,primary_edit=None,backup_edit=None):
    image=Image(total)
    mbr=bytearray(512); mbr[510:]=b'\x55\xaa'; mbr[450]=0xee
    struct.pack_into('<II',mbr,454,1,min(total-1,0xffffffff)); image.put(0,mbr)
    arr=bytearray(128*128)
    for i,(name,first,last) in enumerate(triples):
        e=bytearray(128); e[:16]=c.LINUX_DATA; e[16:32]=uuid.UUID(int=i+1).bytes_le
        struct.pack_into('<QQQ',e,32,first,last,0)
        n=name.encode('utf-16le'); e[56:56+len(n)]=n
        arr[i*128:(i+1)*128]=e
    if rows_edit: rows_edit(arr)
    for pos,array,other,edit in ((1,2,total-1,primary_edit),(total-1,total-33,1,backup_edit)):
        h=bytearray(512); h[:8]=b'EFI PART'; struct.pack_into('<III',h,8,0x10000,92,0)
        struct.pack_into('<4Q',h,24,pos,other,34,total-34)
        h[56:72]=uuid.UUID(int=999).bytes_le
        struct.pack_into('<QIII',h,72,array,128,128,zlib.crc32(arr)&0xffffffff)
        if edit: edit(h)
        h[16:20]=b'\0'*4; struct.pack_into('<I',h,16,zlib.crc32(h[:92])&0xffffffff)
        image.put(pos*512,h); image.put(array*512,arr)
    return image

STUBS=4096   # system_a + system_b, 1 MiB each, at the end of a merged disk

def outer(total=7651328,merged=False,stubs=True,**kw):
    last=total-34
    if merged and stubs:
        rows=c.PREFIX+(('userdata',294912,last-STUBS),('system_a',last-STUBS+1,last-2048),
                       ('system_b',last-2047,last))
    elif merged:
        rows=c.PREFIX+(('userdata',294912,last),)
    else:
        rows=c.PREFIX+c.STOCK_DATA+(('userdata',5046272,last),)
    return gpt(total,rows,**kw)

def userdata_end(image,merged=True):
    return image.size//512-34-(STUBS if merged else 0)

def sb(size,**kw):
    raw=bytearray(1024); blocks=size//4096
    struct.pack_into('<I',raw,0,8192); struct.pack_into('<I',raw,4,blocks)
    struct.pack_into('<I',raw,24,2); struct.pack_into('<I',raw,32,32768)
    struct.pack_into('<I',raw,40,8192); struct.pack_into('<H',raw,56,0xef53)
    struct.pack_into('<H',raw,58,1); struct.pack_into('<I',raw,76,1)
    struct.pack_into('<H',raw,88,256); struct.pack_into('<III',raw,92,0x3c,0x242,0x7b)
    raw[104:120]=uuid.UUID(int=15).bytes_le
    for offset,value,fmt in kw.get('fields',[]): struct.pack_into(fmt,raw,offset,value)
    return raw

def stock(**kw):
    image=outer(**kw); offset=5046272*512; size=image.size-offset-33*512
    image.put(offset+1024,sb(size)); return image

def nested(merged=True):
    image=outer(merged=merged); start=(294912 if merged else 5046272)*512
    size=(userdata_end(image,merged)+1)*512-start
    inner=gpt(size//512,(('pmOS_boot',2048,264191),('pmOS_root',264192,size//512-34)))
    for off,data in inner.regions: image.put(start+off,data)
    return image

class Tests(unittest.TestCase):
    def denied(self,result):
        for k in ('repair_allowed','mount_allowed','format_allowed','conversion_allowed'):
            self.assertIs(result[k],False)
        self.assertIs(result['amonet_version_verified'],False)
    def test_stock_geometry_and_structural_candidate_never_repair(self):
        r=c.classify(stock()); self.denied(r)
        self.assertEqual(r['userdata'],'stock_geometry_ext4_candidate')
        self.assertFalse(r['ext4']['integrity_verified'])
    def test_actual_capacity_not_constant(self):
        r=c.classify(stock(total=7651328-16384)); self.denied(r)
        self.assertEqual(r['last_usable_lba'],7651328-16384-34)
    def test_nested_in_merged_protected(self):
        r=c.classify(nested()); self.denied(r); self.assertEqual(r['userdata'],'validated_nested_gpt')
    def test_nested_in_stock_geometry_also_protected(self):
        r=c.classify(nested(False)); self.denied(r); self.assertEqual(r['userdata'],'validated_nested_gpt')
    def test_no_mutation_and_bounded_reads(self):
        im=nested(); before=im.digest(); c.classify(im)
        self.assertEqual(before,im.digest()); self.assertLess(sum(n for _,n in im.reads),85000)
        self.assertTrue(any(off>2**31 for off,_ in im.reads))
    def test_merged_layout_needs_the_system_stubs(self):
        # amonet v2's bootloader hangs without a system_a entry, so the first
        # merged layout is recognised - to be repaired - but never accepted.
        r=c.classify(outer(merged=True,stubs=False)); self.denied(r)
        self.assertEqual(r['layout'],'v2_merged_without_stubs')
        self.assertEqual(r['reason'],'source_layout_refused')
        r=c.classify(outer(merged=True)); self.assertEqual(r['layout'],'v2_merged_geometry')
    def test_merged_raw_ext_is_not_stock(self):
        im=outer(merged=True); im.put(294912*512+1024,sb(1024*1024))
        r=c.classify(im); self.denied(r); self.assertEqual(r['reason'],'merged_userdata_never_stock_repair')
    def test_corrupt_inner_primary_and_ext_magic_cannot_fallback(self):
        im=nested(); start=294912*512; im.put(start+512,b'BROKEN!!')
        im.put(start+1024,sb(1024*1024)); r=c.classify(im); self.denied(r)
        self.assertEqual(r['userdata'],'partitioned_or_damaged_container')
    def test_corrupt_inner_backup(self):
        im=nested(); im.put(userdata_end(im)*512+20,b'X'); r=c.classify(im); self.denied(r)
        self.assertNotEqual(r['userdata'],'validated_nested_gpt')
    def test_mbr_partition_with_ext_magic_is_protected(self):
        im=stock(); b=bytearray(512); b[510:]=b'\x55\xaa'; b[450]=0x83
        im.put(5046272*512,b); r=c.classify(im); self.denied(r)
        self.assertEqual(r['userdata'],'partitioned_or_damaged_container')
    def test_corrupt_outer_header(self):
        im=stock(); im.put(512+16,b'\x00'*4)
        r=c.classify(im); self.denied(r); self.assertEqual(r['reason'],'gpt_header_crc')
    def test_capacity_mismatch(self):
        im=stock(); im.size-=512; r=c.classify(im); self.denied(r)
        self.assertEqual(r['reason'],'protective_mbr_extent_or_hybrid')
    def test_entry_crc_corruption(self):
        im=stock(); im.put(1024+56,b'X'); r=c.classify(im); self.denied(r)
        self.assertEqual(r['reason'],'gpt_arrays_disagree')
    def test_pair_disagreement_with_valid_crcs(self):
        im=outer(backup_edit=lambda b:b.__setitem__(slice(56,72),uuid.UUID(int=777).bytes_le))
        self.assertEqual(c.classify(im)['reason'],'gpt_pair_disagreement')
    def test_duplicate_partition_guid(self):
        im=outer(rows_edit=lambda a:a.__setitem__(slice(128+16,128+32),a[16:32]))
        self.assertEqual(c.classify(im)['reason'],'duplicate_partition_guid')
    def test_zero_partition_guid(self):
        im=outer(rows_edit=lambda a:a.__setitem__(slice(16,32),b'\0'*16))
        self.assertEqual(c.classify(im)['reason'],'zero_partition_guid')
    def test_zero_disk_guid(self):
        im=outer(primary_edit=lambda a:a.__setitem__(slice(56,72),b'\0'*16))
        self.assertEqual(c.classify(im)['reason'],'zero_disk_guid')
    def test_entry_hole(self):
        im=outer(rows_edit=lambda a:a.__setitem__(slice(128,256),b'\0'*128))
        self.assertEqual(c.classify(im)['reason'],'gpt_entry_hole')
    def test_dirty_unused_entry(self):
        im=outer(rows_edit=lambda a:a.__setitem__(20*128+60,1))
        self.assertEqual(c.classify(im)['reason'],'nonzero_unused_entry')
    def test_overlap(self):
        im=outer(rows_edit=lambda a:struct.pack_into('<Q',a,128+32,4095))
        self.assertEqual(c.classify(im)['reason'],'partition_overlap')
    def test_partition_bounds(self):
        im=outer(rows_edit=lambda a:struct.pack_into('<Q',a,32,1))
        self.assertEqual(c.classify(im)['reason'],'partition_bounds')
    def test_exact_shipped_amonet_outer_pmbr_variant(self):
        im=stock(); im.put(446,bytes.fromhex('00000200eeffffff01000000ffffffff'))
        r=c.classify(im); self.denied(r)
        self.assertEqual(r['pmbr'],'amonet_saturated_32bit')
        self.assertEqual(r['layout'],'v2_stock_geometry')
        with self.assertRaises(c.Rejected): c.validate_gpt(im)
    def test_nearby_nonstandard_pmbr_variant_refused(self):
        im=stock(); im.put(446,bytes.fromhex('00000200eeffffff01000000feffffff'))
        self.assertEqual(c.classify(im)['reason'],'protective_mbr_extent_or_hybrid')
    def test_hybrid_mbr(self):
        im=outer(); im.put(462,b'\x80'); self.assertEqual(c.classify(im)['reason'],'protective_mbr_extent_or_hybrid')
    def test_wrong_entry_type_and_attributes_refused(self):
        for fn in (lambda a:a.__setitem__(0,0xff), lambda a:struct.pack_into('<Q',a,48,1)):
            r=c.classify(outer(rows_edit=fn)); self.denied(r); self.assertEqual(r['layout'],'unknown_geometry')
    def test_legacy_name_refused_no_migration(self):
        def rename(a):
            a[9*128+56:10*128]=b'\0'*72
            a[9*128+56:9*128+56+16]='boot_a_x'.encode('utf-16le')
        r=c.classify(outer(rows_edit=rename)); self.denied(r)
        self.assertEqual(r['layout'],'legacy_or_intermediate_refused')
    def test_ext_unclean_unsupported_and_oversized(self):
        for fields,reason in (([(58,2,'<H')],'ext_unclean_or_recovery_needed'),
                              ([(96,0x246,'<I')],'ext_unclean_or_recovery_needed'),
                              ([(96,0x10242,'<I')],'ext_features_unqualified'),
                              ([(100,0x400,'<I')],'ext_features_unqualified'),
                              ([(4,0xffffffff,'<I')],'ext_exceeds_container')):
            im=outer(); im.put(5046272*512+1024,sb(1024*1024,fields=fields))
            r=c.classify(im); self.denied(r); self.assertEqual(r['reason'],reason)
    def test_missing_capture_is_unknown_not_zeros(self):
        im=outer(); cap=c.CaptureReader(im.size,sorted(im.regions)); r=c.classify(cap)
        self.denied(r); self.assertEqual(r['reason'],'missing_capture_range')
    def test_reader_range_overlap_refused(self):
        with self.assertRaises(c.Rejected): c.CaptureReader(100,[(0,b'ab'),(1,b'xx')])
    def test_nonsector_capacity_refused(self):
        im=outer(); im.size+=1; self.assertEqual(c.classify(im)['reason'],'capacity_not_512_sector_aligned')
    def test_regular_file_reader_readonly_and_detects_change(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'fixture.img'; p.write_bytes(b'\0'*4096)
            reader=c.FileReader(p)
            try:
                self.assertEqual(reader._file.mode,'rb')
                self.assertEqual(reader.read(4090,6),b'\0'*6)
                with self.assertRaises(c.Rejected): reader.read(4090,7)
                p.write_bytes(b'\0'*4097)
                with self.assertRaises(c.Rejected): reader.unchanged()
            finally: reader.close()
    def test_regular_reader_rejects_directory(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(c.Rejected): c.FileReader(d)

if __name__=='__main__': unittest.main()
