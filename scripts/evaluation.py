'''

'''
import os
import gdal
import ogr
import time
import random
import glob
import fnmatch
import numpy as np
import pandas as pd
from gdalconst import *
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
from scipy import stats

import mosaic_by_tsa as mosaic
import aggregate_stem as aggr
from extract_xy_by_tsa import calc_row_stats

#gdal.UseExceptions()


def find_files(search_dir, search_str, eval_scales):
    
    files = [(s, glob.glob(os.path.join(search_dir, search_str % s))[0])
    for s in eval_scales]
    
    return files


def get_dif_map(ar_pred, ar_targ, nodata_p, nodata_t):
    
    mask_p = ar_pred == nodata_p
    mask_t = ar_targ == nodata_t
    nans = np.logical_or(mask_p, mask_t)
    
    dif = ar_pred - ar_targ
    dif[nans] = nodata_p
    
    return dif, nans


def get_overlapping_polys(src_shp, ovr_shp, out_shp):
    '''
    Write a shapefile of all features in ds_src that touch ds_ovr
    '''
    ds_src = ogr.Open(src_shp)
    if ds_src == None:
        print 'Shapefile does not exist or is not valid:\n%s' % src_shp
        return None
    lyr_src = ds_src.GetLayer()
    srs_src = lyr_src.GetSpatialRef()
    
    ds_ovr = ogr.Open(ovr_shp)
    if ds_ovr == None:
        print 'Shapefile does not exist or is not valid:\n%s' % ovr_shp
        return None
    lyr_ovr = ds_ovr.GetLayer()
    
    # Create the output dataset
    driver = ds_src.GetDriver()
    if os.path.exists(out_shp):
        os.remove(out_shp)   
    ds_out = driver.CreateDataSource(out_shp)
    lyr_out = ds_out.CreateLayer(os.path.basename(out_shp)[:-4], srs_src, geom_type=ogr.wkbMultiPolygon) 
    lyr_out_def = lyr_out.GetLayerDefn()
    
    #Get field definitions
    lyr_src_def = lyr_src.GetLayerDefn()
    for i in range(lyr_src_def.GetFieldCount()):
        field_def = lyr_src_def.GetFieldDefn(i)
        lyr_out.CreateField(field_def) 
    
    print 'Finding overlapping features...'
    t0 = time.time()
    # Loop through each feautre and check for overlap
    #for i in xrange(lyr_src.GetFeatureCount()):
    feat_src = lyr_src.GetNextFeature()
    while feat_src:
        t1 = time.time()
        geom_src = feat_src.GetGeometryRef()
        
        for j in xrange(lyr_ovr.GetFeatureCount()):
            feat_ovr = lyr_ovr.GetFeature(j)
            geom_ovr = feat_ovr.GetGeometryRef()
            
            # If there's any overlap, add the feature to the lyr_out
            if geom_ovr.Intersect(geom_src):
                feat_out = ogr.Feature(lyr_out_def)
                feat_out.SetGeometry(geom_src)
                # Get the fields from the source
                for i in range(lyr_out_def.GetFieldCount()):
                    feat_out.SetField(lyr_out_def.GetFieldDefn(i).GetNameRef(), feat_src.GetField(i))
                lyr_out.CreateFeature(feat_out)
                feat_out.Destroy()
                feat_ovr.Destroy()
                feat_ovr = lyr_ovr.GetNextFeature()
                break
            else:
                feat_ovr.Destroy()
                feat_ovr = lyr_ovr.GetNextFeature()
                
        feat_src.Destroy()
        feat_src = lyr_src.GetNextFeature()
    
    print 'Total time: %.1f\n' % (time.time() - t0)
    
    ds_src.Destroy()
    ds_ovr.Destroy()
    lyr_src = None
    lyr_ovr = None
    
    print 'Shapefile written to: ', out_shp     


def feature_to_mask(feat, x_res, y_res):
    '''
    Create a mask from a feature. Useful for generating a single mask of 
    something like a vector layer of hexagons where the mask will be identical
    for every iteration of zonal stats.
    '''
    # Get drivers
    mem_driver = ogr.GetDriverByName('Memory')
    ras_driver = gdal.GetDriverByName('MEM')
    
    # Create the vector layer in memory
    ds_mem = mem_driver.CreateDataSource('out')
    lyr_mem = ds_mem.CreateLayer('poly', None, ogr.wkbPolygon)
    lyr_mem.CreateFeature(feat.Clone())
    
    # Calculate the pixel size
    geom = feat.GetGeometryRef()
    x1, x2, y1, y2 = geom.GetEnvelope()
    xsize = abs(int((x2 - x1)/x_res))
    ysize = abs(int((y2 - y1)/y_res))
    
    # Create the raster datasource and rasterize the vector layer
    tx = x1, x_res, 0, y2, 0, y_res
    ds_ras = ras_driver.Create('', xsize, ysize, 1, gdal.GDT_Byte)
    ds_ras.SetGeoTransform(tx)
    gdal.RasterizeLayer(ds_ras, [1], lyr_mem, burn_values=[1])
    ar = ds_ras.ReadAsArray()
    ar = ar.astype(bool)
    
    ds_ras = None
    ds_mem.Destroy()
    
    return ar
    

def get_zone_inds(ar_size, zone_size, tx, feat):
    '''
    Return the array offset indices for pixels overlapping a feature from a 
    vector dataset. Array indices are returned as (upper_row, lower_row, left_col,_right col)
    to be used to index an array as [upper_row : lower_row, left_col : right_col]
    '''
    geom = feat.GetGeometryRef()
    x1, x2, y1, y2 = geom.GetEnvelope()
    
    # Get the feature ul x and y, and calculate the pixel offset
    ar_ulx, x_res, x_rot, ar_uly, y_rot, y_res = tx
    x_sign = x_res/abs(x_res)
    y_sign = y_res/abs(y_res)
    f_ulx = min([x0/x_sign for x0 in [x1, x2]])/x_sign
    f_uly = min([y0/y_sign for y0 in [y1, y2]])/y_sign
    offset = aggr.calc_offset((ar_ulx, ar_uly), (f_ulx, f_uly), tx)

    # Get the inds for the overlapping portions of each array
    a_inds, m_inds = mosaic.get_offset_array_indices(ar_size, zone_size, offset)
    
    return a_inds, m_inds


def calc_rmspe(x, y):
    ''' Return root mean square percentage error of y with respect to x'''
    rmspe = np.sqrt(((100 * (x - y)/x) ** 2).mean())
    
    return rmspe


def calc_rmse(x, y):
    rmse = np.sqrt(((100 * (x - y)) ** 2).mean())
    
    return rmse


def calc_agree_coef(x, y, mean_x, mean_y):
    ''' 
    Return the agreement coefficient, the systematic agreement, and
    unsystematic agreement
    '''
    # Calc agreement coefficient (ac)
    ssd  = np.sum((x - y) ** 2)
    dif_mean = abs(mean_x - mean_y)
    spod = np.sum((dif_mean + np.abs(x - mean_x)) * (dif_mean + np.abs(y - mean_y)))
    ac = 1 - (ssd/spod)
    
    # Calc GMFR regression
    r , _ = stats.pearsonr(x, y)
    b = (r/abs(r)) * (np.sum((y - mean_y)**2)/np.sum((x - mean_x)**2)) ** (1./2)
    a = mean_y - b * mean_x
    d = 1./b
    c = -float(a)/b
    gmfr_y = a + b * x
    gmfr_x = c + d * y
    
    # Calc systematic and unsystematic sum of product-difference
    spd_u = np.sum(np.abs(x - gmfr_x) * (np.abs(y - gmfr_y)))
    spd_s = ssd - spd_u
    
    # Calc systematic and unsystematic AC
    ac_u = 1 - (spd_u/spod)
    ac_s = 1 - (spd_s/spod)
    
    return ac, ac_s, ac_u, ssd, spod


def zonal_stats(ar, shp, tx, nodata, stats, unique_mask=False):
    '''
    Calculate zonal stats within polygons from shp
    '''
    ds = ogr.Open(shp)
    lyr = ds.GetLayer()
    
    # For each feature, get a mask and calculate stats for the masked part of the array
    feat = lyr.GetNextFeature()
    ul_x, x_res, x_rot, ul_y, y_rot, y_res = tx
    zone_mask = feature_to_mask(feat, x_res, y_res) # Mask is always the same for hex
    zonal_stats = []
    print zone_mask.size

    while feat:
        # If each feature is a unique shape and/or size
        if unique_mask:
            zone_mask = feature_to_mask(feat, x_res, y_res)
        a_inds, m_inds = get_zone_inds(ar.shape, zone_mask.shape, tx, feat)
        
        # Get a rectanglular subset of each array
        ar_sub = ar[a_inds[0]:a_inds[1], a_inds[2]:a_inds[3]]
        
        # Mask out the pixels outside the feature
        m_zone = zone_mask[m_inds[0]:m_inds[1], m_inds[2]:m_inds[3]]
        a_mask = ar_sub != nodata
        a_zone = ar_sub[m_zone & a_mask]
        
        # Get stats for this zone
        these_stats = {name: np.apply_along_axis(function, 0, a_zone).ravel()[0] for name, function in stats.iteritems()}
        fid = feat.GetFID()
        these_stats['fid'] = fid
        zonal_stats.append(these_stats)
        #import pdb; pdb.set_trace()
        feat.Destroy()
        feat = lyr.GetNextFeature()

    ds.Destroy()
    df = pd.DataFrame(zonal_stats)
    #import pdb; pdb.set_trace()
    #df = df.reindex(columns=['fid', 'pred_mean', 'targ_mean', 'mean_dif', 'stdv', 'agree_coef', 'AC_sys', 'AC_unsys', 'ssd', 'spod', 'willmott', 'rmspe'] + ['rmspe_%s' % u for l, u in lims])
    return df

   

"""def zonal_stats(ar_pred, ar_targ, ar_diff, ar_stdv, shp, tx, nodata_p, nodata_t, unique_mask=False):
    '''
    Calculate zonal stats within polygons from shp
    '''
    ds = ogr.Open(shp)
    lyr = ds.GetLayer()
    
    # For each feature, get a mask and calculate stats for the masked part of the array
    feat = lyr.GetNextFeature()
    ul_x, x_res, x_rot, ul_y, y_rot, y_res = tx
    zone_mask = feature_to_mask(feat, x_res, y_res) # Mask is always the same for hex
    t_range = ar_targ[ar_targ != nodata_t].max() - ar_targ[ar_targ != nodata_t].min() # For calculating RMSPE
    zonal_stats = []
    #stats_targ = []
    c = 0
    while feat:
        # If each feature is a unique shape and/or size
        if unique_mask:
            zone_mask = feature_to_mask(feat, x_res, y_res)
        a_inds, m_inds = get_zone_inds(ar_pred.shape, zone_mask.shape, tx, feat)
        
        # Get a rectanglular subset of each array
        ar_pred_sub = ar_pred[a_inds[0]:a_inds[1], a_inds[2]:a_inds[3]]
        ar_targ_sub = ar_targ[a_inds[0]:a_inds[1], a_inds[2]:a_inds[3]]
        ar_diff_sub = ar_diff[a_inds[0]:a_inds[1], a_inds[2]:a_inds[3]]
        ar_stdv_sub = ar_stdv[a_inds[0]:a_inds[1], a_inds[2]:a_inds[3]]
        
        # Mask out the pixels outside the feature
        m_zone = zone_mask[m_inds[0]:m_inds[1], m_inds[2]:m_inds[3]]
        p_zone = ar_pred_sub[m_zone]
        t_zone = ar_targ_sub[m_zone]
        d_zone = ar_diff_sub[m_zone]
        s_zone = ar_stdv_sub[m_zone]
        
        # Mask out nodata values
        p_zone_mask = p_zone == nodata_p
        t_zone_mask = t_zone == nodata_t
        nans = np.logical_or(p_zone_mask, t_zone_mask)
        p_zone = p_zone[~nans]
        t_zone = t_zone[~nans]
        d_zone = d_zone[~nans]
        s_zone = s_zone[~p_zone_mask]
        
        fid = feat.GetFID()
        # Get prediction stats
        mean_p = p_zone.mean()
        mean_t = t_zone.mean()
        mean_d = mean_p - mean_t
        #mean_d = d_zone.mean() #Mean of difference arrray within zone
        
        # Calc root mean square percentage error
        rmspe = calc_rmspe(t_zone, p_zone)
        
        # Calc aggreement coefficient (AC), systematic AC, and unsystematic AC
        ac, ac_s, ac_u, ssd, spod = calc_agree_coef(t_zone, p_zone, mean_t, mean_p)
        
        # Calc Willmott's Index of Agreement (d)
        pe = np.sum(np.abs(t_zone - mean_t) + np.abs(p_zone - mean_t) ** 2) #potential error
        d = 1 - (ssd/pe)
        
        these_stats = {'fid': fid,
                      'pred_mean': mean_p,
                      'targ_mean': mean_t,
                      'mean_dif': mean_d,
                      'stdv': s_zone.mean()/100,
                      'rmspe': rmspe,
                      'agree_coef': ac,
                      'AC_sys': ac_s,
                      'AC_unsys': ac_u,
                      'ssd': ssd,
                      'spod': spod,
                      'willmott': d
                      }
                      
        # Get stats for quantiles
        bin_sz = t_range/10
        lims = [(-1, 0)] + [(i, i + bin_sz) for i in range(0, t_range, bin_sz)]
        #quantile_vals = {}
        for lower, upper in lims:
            dec_mask = (t_zone <= lower) | (t_zone > upper) | (p_zone <= lower) | (p_zone > upper)
            # If there aren't any values in this bin, set rmspe to nodata value
            if np.all(~dec_mask):
                this_rmspe = nodata_p
            else:
                this_t = t_zone[~dec_mask]
                this_p = p_zone[~dec_mask]
                #quantile_vals['targ_%s' % upper] = this_t
                #quantile_vals['pred_%s' % upper] = this_p
                this_rmspe = calc_rmspe(this_t, this_p)
            field = 'rmspe_%s' % upper
            these_stats[field] = this_rmspe
            
        zonal_stats.append(these_stats)
        
        feat.Destroy()
        feat = lyr.GetNextFeature()
        c += 1

    ds.Destroy()
    
    df = pd.DataFrame(zonal_stats)
    df = df.reindex(columns=['fid', 'pred_mean', 'targ_mean', 'mean_dif', 'stdv', 'agree_coef', 'AC_sys', 'AC_unsys', 'ssd', 'spod', 'willmott', 'rmspe'] + ['rmspe_%s' % u for l, u in lims])
    return df#"""
    
    
def df_to_shp(df, in_shp, out_path, copy_fields=True):
    '''
    Write a new shapefile with features from in_shp and attributes from df
    '''
    if 'fid' not in [c.lower() for c in df.columns]:
        print 'Warning: no FID column found in dataframe. Using index of'+\
        ' dataframe instead'
        df['fid'] = df.index
    df.set_index('fid', drop=True, inplace=True)
         
    # Get info from ds_in
    ds_in = ogr.Open(in_shp)
    lyr_in = ds_in.GetLayer()
    srs = lyr_in.GetSpatialRef()
    lyr_in_def = lyr_in.GetLayerDefn()

    # Make new shapefile and datasource, and then a layer from the datasource
    driver = ogr.GetDriverByName('ESRI Shapefile')
    try: ds_out = driver.CreateDataSource(out_path)
    except: print 'Could not create shapefile with out_path: \n', out_path
    lyr = ds_out.CreateLayer(os.path.basename(out_path)[:-4], srs, geom_type=lyr_in.GetGeomType())
    
    # Copy the schema of ds_in
    if copy_fields:
        for i in range(lyr_in_def.GetFieldCount()):
            field_def = lyr_in_def.GetFieldDefn(i)
            lyr.CreateField(field_def)
    
    # Add fields for each of the columns of df 
    for c in df.columns:
        dtype = str(df[c].dtype).lower()
        if 'int' in dtype: lyr.CreateField(ogr.FieldDefn(c, ogr.OFTInteger))
        elif 'float' in dtype: lyr.CreateField(ogr.FieldDefn(c, ogr.OFTReal))
        else: # It's a string
            #import pdb; pdb.set_trace()
            width = df[c].apply(len).max() + 10
            field = ogr.FieldDefn(c, ogr.OFTString)
            field.SetWidth(width)
            lyr.CreateField(field)
    
    lyr_out_def = lyr.GetLayerDefn() # Get the layer def with all the new fields
    for fid, row in df.iterrows():
        # Get the input feature and create the output feature
        feat_in = lyr_in.GetFeature(fid)
        feat_out = ogr.Feature(lyr_out_def)
        feat_out.SetFID(fid)
        
        [feat_out.SetField(name, val) for name, val in row.iteritems()]
        if copy_fields:
            [feat_out.SetField(lyr_in_def.GetFieldDefn(i).GetName(), feat_in.GetField(i)) for i in range(lyr_in_def.GetFieldCount())]
        geom = feat_in.GetGeometryRef()
        feat_out.SetGeometry(geom.Clone())
        lyr.CreateFeature(feat_out)
        #import pdb; pdb.set_trace()
        feat_out.Destroy()
        feat_in.Destroy()
    
    ds_out.Destroy()
    '''Maybe check that all fids in lyr_in were used'''
    
    # Write a .prj file so ArcGIS doesn't complain when you load the shapefile
    srs.MorphToESRI()
    prj_file = out_path.replace('.shp', '.prj')
    with open(prj_file, 'w') as prj:
        prj.write(srs.ExportToWkt()) 
    
    print 'Shapefile written to: \n', out_path


def scatter_plot(x, y, xlab, ylab, out_dir):
    
    plt.scatter(x, y)
    plt.xlabel(xlab)
    plt.ylabel(ylab)
    plt.savefig(os.path.join(out_dir, xlab + '_vs_' + ylab + '.png'))
    

def confusion_matrix(ar_p, ar_t, bins=10, out_txt=None, samples=None):
    ''' 
    Return a dataframe of a confusion matrix of binned continuous values
    '''
    # Check if bins is an int (could be an iterable of bin ranges). If so,
    #   calcualte bin ranges.
    if type(bins) == int:
        t_range = (ar_t.max() - ar_t.min())
        bin_sz = t_range/bins
        bins = [(-1, 0)] + [(i, i + bin_sz) for i in xrange(0, t_range, bin_sz)]
    
    if type(samples) == pd.core.frame.DataFrame:
        p_samples = ar_p[samples.row, samples.col]
        t_samples = ar_t[samples.row, samples.col]
    else:
        p_samples = ar_p
        t_samples = ar_t
    
    
    # For each bin in the target array, count how many pixels are in each bin
    #   in the prediction array.
    cols = {}
    labels = []
    t0 = time.time()
    for l, u in bins:
        print 'Getting counts for truth class from %s to %s' % (l, u)
        t1 = time.time() 
        label = '%s_%s' % (l, u)
        labels.append(label)
        #t_mask = (ar_t > l) & (ar_t <= u) # Create a mask of bin values from target
        t_mask = (t_samples > l) & (t_samples <= u) # Create a mask of bin values from target
        
        counts = []
        for l, u in bins:
            this_p = p_samples[t_mask & (ar_p > l) & (ar_p <= u)]
            counts.append(len(this_p))
        
        #rows.append(counts)
        cols[label] = counts
        print 'Time for this class: %.1f seconds\n' % (time.time() - t1)
        
    df = pd.DataFrame(cols, columns=labels)
    df['bin'] = labels
    df = df.set_index('bin')
    
    # Calculate user's and producer's accuracy
    correct_list = []
    for l in labels:
        correct = df.ix[l, l]
        correct_list.append(correct)
        df.ix[l, 'user'] = round(100 * float(correct)/df.ix[l].sum(), 1)
        df.ix['producer', l] = round(100 * float(correct)/df[l].sum(), 1)
    
    for l in labels[1:]:
        correct = df.ix[l, l]
        correct_list.append(correct)
        df.ix[l, 'user_no0'] = round(100 * float(correct)/df.ix[l, labels[1]:].sum(), 1)
        df.ix['producer_no0', l] = round(100 * float(correct)/df.ix[labels[1]:, l].sum(), 1)
    
    # Calc overall accuracy and kappa coefficient
    total_pxl = df.ix[labels, labels].values.sum()
    acc_o = sum(correct_list)/total_pxl # observed accuracy
    marg_t = df.ix[labels, labels].sum(axis=0)
    marg_p = df.ix[labels, labels].sum(axis=1)
    acc_e = ((marg_t * marg_p)/total_pxl).sum()/total_pxl
    kappa = (acc_o - acc_e)/(1 - acc_e) # Expected accuracy
    df.ix['producer', 'user'] = round(100 * acc_o, 1) 
    df.ix['kappa', 'kappa'] = kappa
    
    total_pxl_0 = df.ix[labels[1:], labels[1:]].values.sum()
    acc_o_0 = sum(correct_list[1:])/total_pxl_0
    marg_t_0 = df.ix[labels[1:], labels[1:]].sum(axis=0)
    marg_p_0 = df.ix[labels[1:], labels[1:]].sum(axis=1)
    acc_e_0 = ((marg_t_0 * marg_p_0)/total_pxl_0).sum()/total_pxl_0
    kappa_0 = (acc_o_0 - acc_e_0)/(1 - acc_e_0)
    df.ix['producer_no0', 'user_no0'] = round(100 * acc_o_0, 1) 
    df.ix['kappa_no0', 'kappa_no0'] = kappa_0
    #df.ix['producer_no0', 'user_no0'] = round(100 * sum(correct_list[1:])/df.ix[labels[1:], labels[1:]].values.sum(), 1)
    
    if out_txt:
        df.to_csv(out_txt, sep='\t')
        print 'Dataframe written to: ', out_txt 
    
    print 'Total time: %.1f minutes' % ((time.time() - t0)/60)
    return df
    

def confusion_matrix_by_area(ar_p, ar_t, samples, p_nodata, t_nodata, mask=None, bins=10, out_txt=None, match=False, target_col=None):
    ''' 
    Return a dataframe of a confusion matrix of binned continuous values
    '''
    # Check if bins is an int (could be an iterable of bin ranges). If so,
    #   calcualte bin ranges.
    t0 = time.time()
    if type(bins) == int:
        t_range = (ar_t.max() - ar_t.min())
        bin_sz = t_range/bins
        bins = [(-1, 0)] + [(i, i + bin_sz) for i in xrange(0, t_range, bin_sz)]
    
    #p_samples = ar_p[samples.row, samples.col]
    print 'Getting average prediction sample vals for 3 x 3 kernel... '
    t1 = time.time()
    row_dirs = [-1,-1,-1, 0, 0, 0, 1, 1, 1]
    col_dirs = [-1, 0, 1,-1, 0, 1,-1, 0, 1]
    kernel_rows = [row + d + 1 for row in samples.row for d in row_dirs] #+1 because buffering at edges
    kernel_cols = [col + d + 1 for col in samples.col for d in col_dirs]
    ar_buf = np.full([dim + 2 for dim in ar_p.shape], p_nodata, dtype=np.int32)
    ar_buf[1:-1, 1:-1] = ar_p
    p_kernel = ar_buf[kernel_rows, kernel_cols].reshape(len(samples), len(row_dirs))
    ar_buf[1:-1, 1:-1] = ar_t
    t_kernel = ar_buf[kernel_rows, kernel_cols].reshape(len(samples), len(row_dirs))
    #del ar_buf
    if not match:
        test_stats = pd.DataFrame(calc_row_stats(p_kernel, 'continuous', 'value', p_nodata))
        sample_mask = ~test_stats.value.isnull().values # Where value is not null
        p_samples = test_stats.value.values # Get values as np array
        p_samples = p_samples[sample_mask].astype(np.int32)
        del test_stats, p_kernel
        
        #if target_col:
        #    t_samples = samples.ix[sample_mask, target_col]
        #else:
        test_stats = pd.DataFrame(calc_row_stats(t_kernel, 'continuous', 'value', t_nodata))
        t_samples = test_stats.value.values[sample_mask].astype(np.int32)
            #t_samples = t_samples[sample_mask].astype(np.int32)
        #import pdb; pdb.set_trace()
        print 'Time to get samples: %.1f seconds\n' % (time.time() - t1)#'''
        
    else:
        print 'Matching...'
        
        #p_kernel = p_kernel.astype(float)
        #p_kernel[p_kernel == p_nodata] = np.nan #Insulate nodata pixels
        #sample_mask = ~np.isnan(p_kernel).all(axis=1)
        p_samples = ar_p[samples.row, samples.col]
        sample_mask = p_samples != p_nodata
        p_samples = p_samples[sample_mask]
        n_samples = p_samples.size
        t_kernel = t_kernel[sample_mask,:].reshape(n_samples, len(row_dirs)).astype(float)
        t_kernel[t_kernel == t_nodata] = np.nan
        
        # Find the pixel in each kernel with the lowest difference
        #dif = np.abs(p_kernel - t_kernel)
        # Subtract sampled prediction value from each pixel in the kernel
        dif = np.abs(np.apply_along_axis(lambda x: x - p_samples, axis=0, arr=t_kernel))
        dif[np.isnan(dif)] = dif.max() + 1#can't keep as nan because some rows could be all nan so nanargmin() with raise an error
        pxl_ind = np.argmin(dif, axis=1)
        #p_samples = p_kernel[sample_mask, pxl_ind]
        t_samples = t_kernel[xrange(n_samples), pxl_ind]
        #import pdb; pdb.set_trace()
        print 'Time to get samples: %.1f seconds\n' % (time.time() - t1)
    #t_samples = ar_t[samples.row, samples.col]#[sample_mask]
    
    ar_p = ar_p[~mask]
    ar_t = ar_t[~mask]
    n_pixels = ar_p.size
    
    # For each bin in the target array, count how many pixels are in each bin
    #   in the prediction array.
    #rows = []
    cols = {}
    labels = []
    sample_counts = []
    total_counts  = []
    #upper = np.array([u for l, u in bins]) + 1
    #p_class = np.clip(np.digitize(p_samples, upper, right=False), 1, 10)
    #t_class = np.clip(np.digitize(t_samples, upper, right=False), 1, 10)
    #import pdb; pdb.set_trace()
    #cm= metrics.confusion_matrix(t_class, p_class)
    #kappa = metrics.cohen_kappa_score(t_class, p_class)
    #labels = ['%s_%s' % (l, u) for l, u in bins]
    for l, u in bins:
        print 'Getting counts for truth class from %s to %s' % (l, u)
        t2 = time.time() 
        label = '%s_%s' % (l, u)
        labels.append(label)
        #t_mask = (ar_t > l) & (ar_t <= u) # Create a mask of bin values from target
        t_mask = (t_samples <= u) & (t_samples > l)# Create a mask of bin values from target
        p_mask = (p_samples <= u) & (p_samples > l)
        
        counts = []
        for this_l, this_u in bins:
            this_p_mask = (p_samples <= this_u) & (p_samples > this_l)
            this_p = p_samples[t_mask & this_p_mask]
            counts.append(len(this_p))
        
        total_count = len(ar_p[(ar_p <= u) & (ar_p > l)])#total pixels in bin
        n_samples = len(p_samples[p_mask])#total samples in this bin
        counts = counts #+ [n_samples, total_count] 
        
        #rows.append(counts)
        cols[label] = counts
        total_counts.append(total_count)
        sample_counts.append(n_samples)
        
        print 'Time for this class: %.1f seconds\n' % (time.time() - t2)
    
    df = pd.DataFrame(cols)#, columns=labels + ['n_samples', 'total'])
    df['total'] = total_counts
    df['n_samples'] = sample_counts#'''
    
    df['bin'] = labels
    df = df.set_index('bin')
    
    # Calculate proportional accuracy
    df['pct_area'] = df.total/float(n_pixels)
    df.ix[labels, labels] = (df.ix[labels, labels] / df.n_samples) * df.pct_area
    
    # Calculate user's and producer's accuracy
    correct_list = []
    for l in labels:
        correct = df.ix[l, l]
        correct_list.append(correct)
        df.ix[l, 'user'] = round(100 * float(correct)/df.ix[l, labels].sum(), 1)
        df.ix['producer', l] = round(100 * float(correct)/df.ix[labels, l].sum(), 1)
    
    correct_list_0 = []
    for l in labels[1:]:
        correct = df.ix[l, l]
        correct_list_0.append(correct)
        df.ix[l, 'user_no0'] = round(100 * float(correct)/df.ix[l, labels[1:]].sum(), 1)
        df.ix['producer_no0', l] = round(100 * float(correct)/df.ix[labels[1:], l].sum(), 1)
    
    # Calc overall accuracy and kappa coefficient
    total_pxl = df.ix[labels, labels].values.sum()
    acc_o = sum(correct_list)/total_pxl # observed accuracy
    marg_t = df.ix[labels, labels].sum(axis=0)
    marg_p = df.ix[labels, labels].sum(axis=1)
    acc_e = ((marg_t * marg_p)/total_pxl).sum()/total_pxl
    kappa = (acc_o - acc_e)/(1 - acc_e) # Expected accuracy
    df.ix['producer', 'user'] = round(100 * acc_o, 1) 
    df.ix['producer', 'kappa'] = round(kappa, 3)
    
    total_pxl_0 = df.ix[labels[1:], labels[1:]].values.sum()
    acc_o_0 = sum(correct_list_0)/total_pxl_0
    marg_t_0 = df.ix[labels[1:], labels[1:]].sum(axis=0)
    marg_p_0 = df.ix[labels[1:], labels[1:]].sum(axis=1)
    acc_e_0 = ((marg_t_0 * marg_p_0)/total_pxl_0).sum()/total_pxl_0
    kappa_0 = (acc_o_0 - acc_e_0)/(1 - acc_e_0)
    df.ix['producer_no0', 'user_no0'] = round(100 * acc_o_0, 1) 
    df.ix['producer_no0', 'kappa_no0'] = round(kappa_0, 3)
    #df.ix['producer_no0', 'user_no0'] = round(100 * sum(correct_list[1:])/df.ix[labels[1:], labels[1:]].values.sum(), 1)
    
    disagree_q, total_q = quantity_disagreement(df, labels)
    disagree_a, total_a = allocation_disagreement(df, labels)
    df.ix[labels, 'quanitity'] = disagree_q
    df.ix['producer', 'quanitity'] = total_q
    df.ix[labels, 'allocation'] = disagree_a
    df.ix['producer', 'allocation'] = total_a
    
    if out_txt:
        df.to_csv(out_txt, sep='\t')
        print '\nDataframe written to: ', out_txt 
    
    print '\nTotal time: %.1f minutes' % ((time.time() - t0)/60)
    return df
    

def quantity_disagreement(df, class_labels):
    
    df_temp = df.ix[class_labels, class_labels]
    #df.ix[class_labels, 'quantity'] = [abs(df_temp.ix[l].sum() - df_temp[l].sum()) for l in class_labels]
    #df.ix['quantity', 'quantity'] = df.quantity.sum() / 2
    disagree_q = [abs(df_temp.ix[l].sum() - df_temp[l].sum()) for l in class_labels]
    total_q = sum(disagree_q) / 2
    
    return disagree_q, total_q


def allocation_disagreement(df, class_labels):
    
    df_temp = df.ix[class_labels, class_labels]
    #df.ix[class_labels, 'allocation'] =\
    #[2 * min(df_temp.ix[l].sum() - df_temp.ix[l,l],
    #         df_temp[l].sum() - df.ix[l,l]) for l in class_labels]
    #df.ix['allocation', 'allocation'] = df.quantity.sum()/2
    disagree_a = [2 * min(df_temp.ix[l].sum() - df_temp.ix[l,l], 
                          df_temp[l].sum() - df.ix[l,l]) for l in class_labels]
    total_a = sum(disagree_a) / 2
    
    return disagree_a, total_a
        


def plot_agreement(df, out_png, sys_color='cyan', unsys_color='magenta', class_labels=None, xlabel='Land Cover Class', ylabel='Agreement Coefficient'):
    
    print 'Plotting stats...'
    width = .25
    x_loc = np.arange(len(df)) + width
    bar_s = plt.bar(x_loc - width, df.AC_sys, width, color=sys_color, alpha=.5, edgecolor='none', label='Systematic')
    bar_u = plt.bar(x_loc, df.AC_unsys, width, color=unsys_color, alpha=.5, edgecolor='none', label='Unsystematic')
    bar_r = plt.bar(x_loc + width, df.rmspe/100.0, width, color='.7', edgecolor='none', label='RMSPE')
    
    #ax.set_xticklabels([str(lc) for lc in classes])
    if class_labels: plt.xticks(x_loc + width/2., class_labels, size='small')
    else: plt.xticks(x_loc + width/2., size='small')
    plt.ylabel(ylabel)
    plt.xlabel(xlabel)
    #ax.set_xticks(x_loc + width/2)
    plt.title('Agreement Coefficient Per ' + xlabel)
    plt.xlim(0, max(x_loc) + width * 2)
    plt.ylim(-1.5,1)
    plt.plot((0, max(x_loc) + width * 2), (0,0), color='k')
        
    plt.legend(loc='lower right', frameon=False)
    #plt.legend(bar_s[0], bar_u[0], ['Systematic', 'Unsystematic'])
    
    plt.savefig(out_png)
    plt.clf()
    print 'Plot saved to ', out_png


def evaluate_by_lc(ar_p, ar_t, ar_lc, mask, nodata_lc, out_dir):
    '''
    Return a dataframe of the agreement between ar_p and ar_t by class in ar_lc. 
    '''
    print 'Getting unique values...'
    classes = np.unique(ar_lc[(ar_lc != nodata_lc) & (ar_lc != 0)]) # Take 0's out too
    #import pdb; pdb.set_trace()
    stats = []
    for lc in classes:
        print 'Calculating statistics for class ', lc
        this_mask = (ar_lc == lc) & mask
        this_t = ar_t[this_mask]
        this_p = ar_p[this_mask]
        ac, ac_s, ac_u, ssd, spod = calc_agree_coef(this_t, this_p, this_t.mean(), this_p.mean())
        rmspe = calc_rmspe(this_t, this_p)
        class_stats = {'lc_class': lc, 'aggree_coef': ac, 'AC_sys': ac_s, 'AC_unsys': ac_u, 'rmspe': rmspe}
        stats.append(class_stats)
    
    df = pd.DataFrame(stats).reindex(columns=['lc_class', 'aggree_coef', 'AC_sys', 'AC_unsys', 'rmspe'])
    out_txt = os.path.join(out_dir, 'lc_stats.txt')
    df.to_csv(out_txt, sep='\t', index=False)
    
    out_png = os.path.join(out_dir, 'agreement_per_lc_no0.png')
    class_labels = ['Water', 'Ice/Snow', 'Developed', 'Bare Ground', 'Deciduous Forest', 'Coniferous Forest', 'Shrubland']
    plot_agreement(df, out_png, class_labels=class_labels)
    
    return df    


def plot_bin_agreement(ar_pred, ar_targ, nodata_t, out_dir):
    
    t_range = ar_targ.max() - ar_targ.min()
    bin_sz = t_range/10
    #lims = [(-1, 0)] + [(i, i + bin_sz) for i in range(0, t_range, bin_sz)]   
    lims = [(i, i + bin_sz) for i in range(0, t_range, bin_sz)]
    
    bin_stats = []
    for lower, upper in lims:
        print 'Calculating stats for %s to %s' % (lower, upper) 
        mask = (ar_targ > lower) & (ar_targ <= upper) & (ar_pred > lower) & (ar_pred <= upper)
        this_t = ar_targ[mask]
        this_p = ar_pred[mask]
        mean_t = this_t.mean()
        mean_p = this_p.mean()
        ac, ac_s, ac_u, ssd, spod = calc_agree_coef(this_t, this_p, mean_t, mean_p)
        rmspe = calc_rmspe(this_t, this_p)
        these_stats = {'bin': '%s_%s' % (lower, upper), 'aggree_coef': ac, 'AC_sys': ac_s, 'AC_unsys': ac_u, 'rmspe': rmspe}
        bin_stats.append(these_stats)
    
    df = pd.DataFrame(bin_stats)
    out_txt = os.path.join(out_dir, 'agreement_per_bin.txt')
    df.to_csv(out_txt, sep='\t', index=False)
    
    out_png = os.path.join(out_dir, 'agreement_per_bin_no0.png')
    plot_agreement(df, out_png, class_labels=df.bin.tolist(), xlabel='Bin Range', ylabel='Prediction Value')


def main(pred_path, targ_path, lc_path, mask_path, nodata_p, nodata_t, nodata_lc, search_dir, search_str, eval_scales, out_dir, clip_shp=None):
    
    pxl_scale_dir = os.path.join(out_dir, 'pixel_scale')
    if not os.path.exists(pxl_scale_dir): 
        os.makedirs(pxl_scale_dir)
    
    ds_m = gdal.Open(mask_path)
    tx_m = ds_m.GetGeoTransform()
    ar_m = ds_m.ReadAsArray().astype(np.int32)
    nonforest = ar_m == 1
    ar_m = None
    
    print '\nReading in raster data...\n'
    ds_p = gdal.Open(pred_path)
    ar_p = ds_p.ReadAsArray()
    tx   = ds_p.GetGeoTransform()
    prj  = ds_p.GetProjection()
    driver = ds_p.GetDriver()

    ds_t = gdal.Open(targ_path)
    ar_t = ds_t.ReadAsArray()
    ar_t[ar_t == 0] = nodata_t
    
    ar_t[nonforest] = nodata_t
    ar_p[nonforest] = nodata_p
    
    stdv_path = pred_path.replace('vote', 'stdv')
    ds_stdv = gdal.Open(stdv_path)
    ar_stdv = ds_stdv.ReadAsArray()
    
    print 'Getting difference map...'
    t0 = time.time()
    ar_diff, nans = get_dif_map(ar_p, ar_t, nodata_p, nodata_t)
    ras_ext = pred_path.split('.')[-1]
    dif_path = os.path.join(pxl_scale_dir, 'prediction_minus_target.' + ras_ext)
    mosaic.array_to_raster(ar_diff, tx, prj, driver, dif_path, GDT_Int32, nodata_p)
    print '%.1f seconds\n' % (time.time() - t0)
    
    shps = find_files(search_dir, search_str, eval_scales)
    print 'Calculating stats and plotting for all evaluation scales...'
    for eval_scale, zone_shp in shps:
        
        #If clip_shp is specified, assume that zone shape is unclipped and clip it
        if clip_shp:
            print ('clip_shp given so... getting only features from %s that ' +\
            'overlap %s') % (zone_shp, clip_shp)
            out_shp = zone_shp.replace('.shp', '_%s.shp' % os.path.basename(clip_shp)[:-4])
            get_overlapping_polys(zone_shp, clip_shp, out_shp)
            zone_shp = out_shp
        
        scale_dir = os.path.join(out_dir, 'scale_%s_m' % eval_scale)
        if not os.path.exists(scale_dir): 
            os.mkdir(scale_dir)   
            
        print 'Getting zonal stats for %s scale...' % eval_scale
        t0 = time.time()
        df_stats = zonal_stats(ar_p, ar_t, ar_diff, ar_stdv, zone_shp, tx, nodata_p, nodata_t)
        out_txt = os.path.join(scale_dir, 'zonal_stats_%s.txt' % eval_scale)
        df_stats.to_csv(out_txt, sep='\t', index=False)
        print '%.1f seconds\n' % (time.time() - t0)
        
        print 'Writing stats to shp...'
        t0 = time.time()
        out_shp = os.path.join(scale_dir, 'zonal_stats_%s.shp' % eval_scale)
        df_to_shp(df_stats, zone_shp, out_shp, copy_fields=False)
        print '%.1f seconds\n' % (time.time() - t0)
        
        print 'Making scatter plot for %s scale...' % eval_scale
        t0 = time.time()
        plt.scatter(df_stats.targ_mean, df_stats.pred_mean, alpha=.05)
        plt.xlabel('Target')
        plt.ylabel('Prediction')
        scatter_path = os.path.join(scale_dir, 'scatter_%s.png' % eval_scale)
        plt.savefig(scatter_path)
        print '%.1f seconds\n' % (time.time() - t0)
    
    ar_stdv = None
    ds_stdv = None
    ar_diff = None

    ar_t_data = ar_t[~nans] 
    ar_p_data = ar_p[~nans]    
    print 'Plotting scatter of the 2 maps...'
    t0 = time.time()

    inds = random.sample(xrange(len(ar_t_data)), 100000)
    x = ar_t_data[inds]
    y = ar_p_data[inds]
    plt.scatter(x, y, alpha=.01) 
    plt.xlabel(os.path.basename(targ_path))
    plt.ylabel(os.path.basename(pred_path))
    fig_path = os.path.join(pxl_scale_dir, 'prediction_vs_target_scatter_no0.png')
    plt.savefig(fig_path)
    plt.clf()
    print '%.1f seconds\n' % (time.time() - t0)
        
    # Create 2D histograms
    print 'Plotting 2D histogram...'
    t0 = time.time()
    plt.hist2d(ar_t_data, ar_p_data, bins=50, norm=LogNorm())
    plt.xlabel(os.path.basename(targ_path))
    plt.ylabel(os.path.basename(pred_path))
    plt.colorbar()
    fig_path = os.path.join(pxl_scale_dir, 'prediction_vs_target_2Dhistogram_no0.png')
    plt.savefig(fig_path)
    plt.clf()
    print '%.1f seconds\n' % (time.time() - t0)
    
    print 'Evaluating by land cover class...'
    t0 = time.time()
    ds_lc = gdal.Open(lc_path)
    ar_lc = ds_lc.ReadAsArray()
    df_lc = evaluate_by_lc(ar_p, ar_t, ar_lc, ~nans, nodata_lc, pxl_scale_dir)
    print '%.1f seconds\n' % (time.time() - t0)
    
    print 'Plotting bin stats...'
    t0 = time.time()
    plot_bin_agreement(ar_p_data, ar_t_data, nodata_t, pxl_scale_dir)
    print '%.1f seconds\n' % (time.time() - t0)#'''
    
    print 'Calculating confusion matrix...'
    t0 = time.time()
    out_txt = os.path.join(pxl_scale_dir, 'confusion_matrix.txt')
    ar_t_samples = ar_t_data[inds]
    ar_p_samples = ar_p_data[inds]
    confusion_matrix(ar_p_samples, ar_t_samples, out_txt=out_txt)
    print '%.1f seconds\n' % (time.time() - t0)
    
    ds_p = None
    ds_t = None
    ds_lc = None
    ar_p = None
    ar_t = None
    ar_lc = None
    
    print 'Outputs written to ', out_dir

'''if __name__ == '__main__':
     params = sys.argv[1]
     sys.exit(main(params)) #'''



''' ########## Testing ############# '''
"""src_shp = '/vol/v2/stem/extent_shp/hex50000.shp'
tch_shp = '/vol/v2/stem/extent_shp/CAORWA.shp'
#zone_shp = '/vol/v2/stem/extent_shp/hex50000_CAORWA.shp'
zone_shp = '/vol/v2/stem/extent_shp/hex20000_CAORWA.shp'
eval_scales = [10000, 20000, 50000]
#get_overlapping_polys(src_shp, tch_shp, zone_shp)'''
out_shp = '/vol/v2/stem/imperv/models/imperv_20161012_0958/evaluation_vote/imperv_stdvavg20000.shp'
path = '/vol/v2/stem/imperv/models/imperv_20161012_0958/imperv_20161012_0958_stdv.bsq'
ds = gdal.Open(path)
ar = ds.ReadAsArray()
tx = ds.GetGeoTransform()
stats = {'mean': np.mean, 'count': np.count_nonzero}
df = zonal_stats(ar, zone_shp, tx, 255, stats)
#df['area_change'] = df['mean'] * df['count']
#import pdb; pdb.set_trace()
df_to_shp(df, zone_shp, out_shp)
ar = None
ds = None#"""

#p_path = '/vol/v2/stem/canopy/outputs/canopy_20160311_2209/canopy_20160311_2209_final_vote.bsq'
#t_path = '/vol/v2/stem/canopy/truth_map/canopy2001_CAORWA.bsq'

"""p_path = '/vol/v2/stem/imperv/models/imperv_20160925_1241/imperv_20160925_1241_vote.bsq'
t_path = '/vol/v2/stem/imperv/truth_map/imperv2001_CAORWA.bsq'

lc_path = '/vol/v1/general_files/datasets/spatial_data/states_spatial_data/calorewash_buffer_spatial_data/calorewash_buffer_nlcd2001_landcover_shift.tif'
mask_path = '/vol/v2/stem/canopy/truth_map/nonforest_mask_2001.tif' #Change to nlcd mask
nodata_p = -9999
nodata_t = 255
nodata_lc = 255
out_dir = '/vol/v2/stem/imperv/models/imperv_20160925_1241/evaluation_vote'
search_dir = '/vol/v2/stem/extent_shp'
search_str = 'hex%s_CAORWA.shp'
#main(p_path, t_path, lc_path, mask_path, nodata_p, nodata_t, nodata_lc, search_dir, search_str, eval_scales, out_dir, clip_shp=None)
'''txt = '/vol/v2/stem/canopy/outputs/imperv_20160605_1754/evaluation/agreement_per_decile.txt'
df = pd.read_csv(txt, sep='\t')'''

#mask_path = '/vol/v2/stem/imperv/truth_map/nlcd2001_urbanmask.bsq'
'''mask_path = '/vol/v2/stem/imperv/models/imperv_20160704_1504/imperv_20160725_2002_vote.bsq'
ds_m = gdal.Open(mask_path)
tx_m = ds_m.GetGeoTransform()
ar_m = ds_m.ReadAsArray().astype(np.int32)
ar_m = ar_m == 0
#ar_m = None#'''

ds_p = gdal.Open(p_path)
ar_p = ds_p.ReadAsArray()
#ar_p[ar_m] = 0
#ar_p = ar_p[10000:20000, 5000:15000]
tx = ds_p.GetGeoTransform()
#ar_p[nonforest] = 0
#aggr.mask_array(ar_p, ar_m, tx, tx_m, mask_val=0)

ds_t = gdal.Open(t_path)
ar_t = ds_t.ReadAsArray()
#ar_t[nonforest] = 0
#aggr.mask_array(ar_c, forest_mask, tx, tx_m, mask_val=0)
#ar_c = ar_c[10000:20000, 5000:15000]
mask = (ar_p == -9999) | (ar_t == 255)#'''
bins = [(-1,0), (0,10), (10,20), (20,30), (30,40), (40,50), (50, 60), (60, 70), (70, 80), (80,90), (90,100)]

#out_txt = '/vol/v2/stem/imperv/models/imperv_20160725_2002/evaluation_vote/confusion_allpixels.txt'

'''ar_p_data = ar_p[~mask]
ar_t_data = ar_t[~mask]#'''

smpl_txt = '/vol/v2/stem/imperv/samples/imperv_sample283622_20160917_1846/imperv_sample_test283622_20160917_1846.txt'
samples = pd.read_csv(smpl_txt, sep='\t', index_col='obs_id')
'''ar_t_data = samples.imperv.values
ar_p_data = ar_p[samples.row, samples.col]#'''

#out_txt = os.path.join(out_dir, 'confusion_allpixels.txt')
out_txt = os.path.join(out_dir, 'confusion.txt')
if not os.path.exists(out_dir):
    os.mkdir(out_dir)
#df1 = confusion_matrix(ar_p_data, ar_t_data, bins=bins, out_txt=out_txt)
df = confusion_matrix_by_area(ar_p, ar_t, samples, -9999, mask=mask, bins=bins, out_txt=out_txt)
#'''

#stats = zonal_stats(ar_p, ar_c, out_shp, tx, -9999, 255)
ds_p = None
ds_t = None#'''
ds_m = None
ar_p = None
ar_t = None
ar_m = None
nonforest = None
mask = None
ar_shape = 64348, 27023


out_png = '/vol/v2/stem/canopy/outputs/canopy_20160311_2209/evaluation/agreement_per_bin.png'
#plot_agreement(df, out_png)

#files = find_files(search_dir, search_str, eval_scales)#"""